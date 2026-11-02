"""Restore only into a fresh local directory; serve the existing app on loopback."""
from contextlib import closing
from datetime import datetime
import hashlib
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor, Request
from uuid import uuid4

from PIL import Image
from server.maintenance import validate_database


class RecoveryError(Exception):
    pass


def file_hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def inspect_database(path):
    validate_database(path)
    counts, hashes = {}, {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as con:
        for table in ('users', 'user_sites', 'records', 'media', 'audit', 'imports', 'mutations'):
            digests = [hashlib.sha256(repr(row).encode()).hexdigest()
                       for row in con.execute('select * from ' + table)]
            counts[table] = len(digests)
            hashes[table] = sorted(digests)
        roles = dict(con.execute('select username, role from users where active=1'))
        if roles.get('manager') != 'manager' or roles.get('inspector') != 'inspector':
            raise RecoveryError('В копии нет двух активных тестовых аккаунтов manager и inspector.')
        for content, thumbnail in con.execute('select content, thumbnail from media'):
            for blob in (content, thumbnail):
                with Image.open(io.BytesIO(blob)) as image:
                    image.verify()
                with Image.open(io.BytesIO(blob)) as image:
                    image.load()
    return counts, hashes


def prepare(source, parent):
    source, parent = Path(source).resolve(), Path(parent).resolve()
    if not source.is_file():
        raise RecoveryError('Выберите существующий файл резервной копии SQLite.')
    if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise RecoveryError('Рядом с файлом найдены служебные файлы работающей БД. Выберите отдельную скачанную резервную копию.')
    if str(parent).startswith(('\\\\', '//')) or str(source).startswith(('\\\\', '//')):
        raise RecoveryError('Выберите локальный диск, например C:\\KSU. Сетевая папка не подходит.')
    before = file_hash(source)
    # A unique directory is never reused, even after a failed verification.
    folder = parent / ('Проверка-КСУ-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8])
    folder.mkdir(mode=0o700, parents=False, exist_ok=False)
    staged, restored = folder / 'snapshot.sqlite3', folder / 'test.sqlite3'
    with source.open('rb') as original, staged.open('xb') as output:
        shutil.copyfileobj(original, output)
        output.flush()
        os.fsync(output.fileno())
    if file_hash(staged) != before or file_hash(source) != before:
        raise RecoveryError('Исходный файл изменился во время чтения. Остановите проверку и выберите стабильную копию.')
    expected_counts, expected_hashes = inspect_database(staged)
    # No hard-link requirement: also works on removable FAT/exFAT disks.
    with restored.open('xb'):
        pass
    with closing(sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(restored)) as copy:
            original.backup(copy)
            with copy:
                copy.execute('delete from sessions')
                copy.execute('delete from attempts')
    actual_counts, actual_hashes = inspect_database(restored)
    if (actual_counts, actual_hashes) != (expected_counts, expected_hashes):
        raise RecoveryError('Содержимое восстановленной базы отличается от резервной копии.')
    return {'folder': folder, 'database': restored, 'source': source, 'sha256': before,
            'counts': actual_counts, 'restored': True}


class LocalServer:
    def __init__(self, result, font=None):
        self.result = result
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(('127.0.0.1', 0))
        self.origin = 'http://127.0.0.1:' + str(self.socket.getsockname()[1])
        # Ignore inherited deployment configuration for this isolated process.
        for key in list(os.environ):
            if key.startswith('KSU_'):
                os.environ.pop(key)
        if font:
            os.environ['KSU_PDF_FONT'] = str(font)
        os.environ['KSU_REQUIRE_INITIALIZED'] = '1'
        from server.app import create_app
        import uvicorn
        try:
            app = create_app(result['database'], self.origin, False)
            self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0,
                                        log_config=None, access_log=False, proxy_headers=False,
                                        loop='asyncio', http='h11', ws='none'))
            self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]}, daemon=True)
        except Exception:
            self.socket.close()
            raise

    def start(self):
        self.thread.start()
        client = client_for(self.origin)
        for _ in range(150):
            if not self.thread.is_alive():
                break
            try:
                if api(client, self.origin, '/api/health')[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(.1)
        self.stop()
        raise RecoveryError('Не удалось запустить локальное приложение. Закройте проверку и повторите запуск.')

    def stop(self):
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        self.socket.close()
        if self.thread.is_alive():
            raise RecoveryError('Локальный сервер ещё завершает работу. Не извлекайте диск; закройте приложение позднее.')


def client_for(origin):
    # Never send localhost requests or passwords through a system proxy.
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def api(client, origin, path, data=None, csrf=None):
    headers = {'Origin': origin}
    if csrf:
        headers['X-CSRF-Token'] = csrf
    request = Request(origin + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={**headers, 'Content-Type': 'application/json'})
    try:
        with client.open(request, timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def check_accounts(server, passwords):
    outcomes = {}
    for role in ('manager', 'inspector'):
        client = client_for(server.origin)
        status, content = api(client, server.origin, '/api/login', {'username': role, 'password': passwords[role]})
        if status != 200:
            raise RecoveryError('Вход ' + role + ' не выполнен. Проверьте пароль от резервной копии (HTTP ' + str(status) + ').')
        user = json.loads(content)
        csrf = user['csrf']
        try:
            if user['role'] != role:
                raise RecoveryError('Роль аккаунта не совпадает с ожидаемой.')
            status, content = api(client, server.origin, '/api/state')
            if status != 200:
                raise RecoveryError('Не удалось прочитать записи после входа.')
            state = json.loads(content)
            # Check every image in the role's assigned scope, including thumbnails.
            with closing(sqlite3.connect(server.result['database'])) as con:
                allowed = {r[0] for r in con.execute('select site_id from user_sites where user_id=?', (user['id'],))}
                media = [(ident, bytes(blob), bytes(thumb)) for ident, site, blob, thumb in
                         con.execute('select id, site_id, content, thumbnail from media')
                         if role == 'manager' or site in allowed]
            for ident, blob, thumb in media:
                for suffix, expected in (('', blob), ('?thumbnail=true', thumb)):
                    status, content = api(client, server.origin, '/api/media/' + ident + suffix)
                    if status != 200 or content != expected:
                        raise RecoveryError('Фото или миниатюра недоступны для ' + role + '.')
            for extension, signature in (('json', b'{'), ('xlsx', b'PK'), ('pdf', b'%PDF')):
                status, content = api(client, server.origin, '/api/reports/' + extension + '?kind=findings&period=all')
                if status != 200 or not content.startswith(signature):
                    raise RecoveryError('Не удалось сформировать отчёт ' + extension + ' для ' + role + '.')
            if role == 'inspector':
                if api(client, server.origin, '/api/admin/backup', {}, csrf)[0] != 403:
                    raise RecoveryError('Нарушено ограничение выгрузки базы для inspector.')
            outcomes[role] = {'login': True, 'records': sum(len(state[k]) for k in
                             ('sites', 'questions', 'findings', 'inspections', 'inventories', 'inventoryLines', 'visits')),
                              'images': len(media), 'reports': True}
        finally:
            api(client, server.origin, '/api/logout', {}, csrf)
    return outcomes


def save_summary(result, accounts, confirmations, stopped):
    unchanged = file_hash(result['source']) == result['sha256']
    complete = bool(accounts) and all(confirmations.values()) and unchanged and stopped
    lines = ['КСУ Контроль — локальная проверка восстановления',
             'ИТОГ: ' + ('ПРОВЕРКА ПРОЙДЕНА' if complete else 'ПРОВЕРКА НЕ ЗАВЕРШЕНА'),
             'Восстановление в отдельную папку: успешно',
             'Целостность SQLite и связи: ошибок нет',
             'Записей: ' + str(result['counts']['records']),
             'Изображений с миниатюрами: ' + str(result['counts']['media']),
             'Исходная резервная копия: ' + ('НЕ ИЗМЕНЕНА' if unchanged else 'ИЗМЕНИЛАСЬ — требуется проверка'),
             'SHA-256 исходной копии: ' + result['sha256'],
             'Автоматический вход, доступ к фото и JSON/XLSX/PDF для обоих аккаунтов: ' + ('успешно' if accounts else 'не подтверждено'),
             'Локальный сервер: ' + ('остановлен' if stopped else 'ещё работает')]
    for label, value in confirmations.items():
        lines.append('Проверка владельцем — ' + label + ': ' + ('подтверждено' if value else 'НЕ подтверждено'))
    lines += ['', 'Это локальная проверка копии. Перезапуск и сохранность Amvera ею не подтверждаются.',
              'База, фотографии, пароли и содержание записей в этот итог не включены.',
              'Тестовая папка остаётся на компьютере; не отправляйте её или базу в облако.']
    target = result['folder'] / 'ИТОГ-ПРОВЕРКИ.txt'
    target.write_text('\n'.join(lines), encoding='utf-8-sig')
    return target, complete
