"""Large SQLite BLOB/storage stress; bytes are synthetic, not corporate photos."""
from contextlib import closing
import json
from pathlib import Path
import resource
import sqlite3
import tempfile
import time

from server.database import Database,password_hash
from server.pg_admin import fingerprints
from server.release_backup import snapshot_frozen
from server.release_schema import checksum,prepare_sqlite


def run():
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='ksu-large-release-synthetic-') as directory:
        root=Path(directory);frozen=root/'frozen';frozen.mkdir();source=frozen/'ksu.sqlite3'
        db=Database(source)
        with db.connect() as con:
            for role in ('manager','inspector'):
                con.execute('insert into users values(?,?,?,?,?,1)',(role,'synthetic-'+role,'Synthetic '+role,role,password_hash('synthetic '+role+' password')))
        with closing(sqlite3.connect(source)) as con:
            con.execute('pragma journal_mode=delete')
            for index in range(75):
                con.execute('insert into media values(?,?,?,?,?,?)',
                    ('synthetic-blob-'+str(index),'s1','inspector',bytes([index])*2097152,b'synthetic thumbnail','2026-11-09'))
                con.commit()
            before=fingerprints(con)
        size=source.stat().st_size;assert 150*1024**2<=size<256*1024**2
        original=checksum(source)
        backup=root/'independent/backup.sqlite3';snapshot_frozen(frozen,backup,confirmed=True)
        result=prepare_sqlite(backup,root/'restored',closed_backup=True)
        target=root/'restored/ksu.sqlite3'
        with closing(sqlite3.connect(target)) as con:
            assert fingerprints(con)==before
            assert con.execute('pragma integrity_check').fetchone()[0]=='ok'
            assert not con.execute('pragma foreign_key_check').fetchall()
        assert checksum(source)==original and result['all_original_tables_preserved']
        return {'synthetic_only':True,'input_bytes':size,'backup_bytes':backup.stat().st_size,
            'business_tables_byte_fingerprints_equal':True,'original_unchanged':True,
            'independent_restore_integrity_fk':'ok','elapsed_seconds':round(time.monotonic()-start,2),
            'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'scope':'storage BLOB stress, not JPEG decoding or live-volume WAL safety'}


if __name__=='__main__':print(json.dumps(run(),indent=2))
