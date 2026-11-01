"""Verified, no-overwrite snapshots. Neither a restore nor a backup copies live sessions."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


def validate_database(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as con:
        if con.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Нарушена целостность SQLite')
        if con.execute('pragma foreign_key_check').fetchall():
            raise ValueError('Нарушены связи SQLite')
        required = {'users', 'records', 'media', 'audit', 'sessions', 'imports', 'mutations'}
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if not required <= tables:
            raise ValueError('Это не база КСУ Контроль')
        if not con.execute('select count(*) from users where active=1').fetchone()[0]:
            raise ValueError('База не инициализирована')


def snapshot_database(source, destination):
    source = Path(source).resolve(strict=True)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Файл назначения уже существует')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name('.' + target.name + '.' + uuid4().hex + '.partial')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as copied:
                with copied:
                    original.backup(copied)
                    tables = {r[0] for r in copied.execute("select name from sqlite_master where type='table'")}
                    if not {'sessions', 'attempts'} <= tables:
                        raise ValueError('Это не база КСУ Контроль')
                    copied.execute('delete from sessions')
                    copied.execute('delete from attempts')
        validate_database(temporary)
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        # Hard link is atomic and refuses any pre-existing destination (including a symlink).
        os.link(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
