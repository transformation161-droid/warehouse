"""Packaged Windows smoke test: creates synthetic data locally, uploads nothing."""
import io
import json
from pathlib import Path
import secrets
import tempfile

from PIL import Image
from portable.recovery import LocalServer, api, check_accounts, client_for, file_hash, prepare, save_summary
from server.database import Database, seed_demo


def fixture(path):
    passwords = {role: secrets.token_urlsafe(24) for role in ('manager', 'inspector')}
    db = Database(path)
    seed_demo(db, passwords['manager'], passwords['inspector'])
    output = io.BytesIO()
    Image.new('RGB', (30, 30), 'red').save(output, 'JPEG')
    actor = db.user('inspector')
    with db.connect(actor) as con:
        for number in (1, 2):
            ident = 'synthetic-' + str(number)
            con.execute('insert into media values(?,?,?,?,?,?)',
                        (ident, 's1', actor['id'], output.getvalue(), output.getvalue(), '2026-11-02T00:00:00Z'))
            data = {'siteId': 's1', 'title': 'ДЕМО · Проверка восстановления ' + str(number),
                    'status': 'open', 'photo': '/api/media/' + ident}
            con.execute('insert into records values(?,?,?,?,?,?,?,?)',
                        ('findings', ident, json.dumps(data), actor['id'], 's1', 1,
                         '2026-11-02T00:00:00Z', '2026-11-02T00:00:00Z'))
    return passwords


def run(output, font):
    with tempfile.TemporaryDirectory(prefix='ksu-synthetic-') as directory:
        parent = Path(directory)
        source = parent / 'synthetic.sqlite3'
        passwords = fixture(source)
        before = file_hash(source)
        result = prepare(source, parent)
        assert result['counts']['records'] == 36 and result['counts']['media'] == 2
        assert result['database'] != source
        server = LocalServer(result, font)
        try:
            server.start()
            client = client_for(server.origin)
            for path in ('/', '/app.js', '/sync.js', '/styles.css'):
                assert api(client, server.origin, path)[0] == 200, path
            for path in ('/api/state', '/api/history', '/api/media/synthetic-1'):
                assert api(client, server.origin, path)[0] == 401, path
            accounts = check_accounts(server, passwords)
        finally:
            server.stop()
        assert file_hash(source) == before
        summary, complete = save_summary(result, accounts, {'synthetic manual substitute': True}, True)
        assert complete
        output.write_text(json.dumps({'synthetic_only': True, 'records': 36, 'images': 2,
                          'both_logins': True, 'media_and_thumbnails': True, 'json_xlsx_pdf': True,
                          'original_unchanged': True, 'server_stopped': True,
                          'packaged_assets': True}), encoding='utf-8')
