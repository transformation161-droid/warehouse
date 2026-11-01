import argparse
import getpass
import os
import secrets
import sqlite3
from pathlib import Path
from .database import Database, ROOT, seed_demo

parser=argparse.ArgumentParser(description='КСУ Контроль — локальная демонстрация')
sub=parser.add_subparsers(dest='command',required=True)
init=sub.add_parser('init-demo');init.add_argument('--db',default=str(ROOT/'var/ksu.sqlite3'))
init.add_argument('--generate',action='store_true',help='Сгенерировать тестовые пароли в закрытом файле рядом с БД')
run=sub.add_parser('run');run.add_argument('--port',type=int,default=8000)
backup=sub.add_parser('backup');backup.add_argument('destination');backup.add_argument('--db',default=str(ROOT/'var/ksu.sqlite3'))
args=parser.parse_args()
if args.command=='init-demo':
    if args.generate:
        target=Path(args.db).parent/'demo-accounts.txt'
        if target.exists():parser.error('Файл тестовых аккаунтов уже существует')
        manager_password=secrets.token_urlsafe(18);inspector_password=secrets.token_urlsafe(18)
        seed_demo(Database(args.db),manager_password,inspector_password)
        descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'w') as output:
            output.write('ТОЛЬКО ЛОКАЛЬНАЯ ДЕМОНСТРАЦИЯ. Не добавлять в Git.\n\nРуководитель\nЛогин: manager\nПароль: '+manager_password+'\n\nИсполнитель\nЛогин: inspector\nПароль: '+inspector_password+'\n')
        print('Пароли сохранены локально:',target)
    else:
        seed_demo(Database(args.db),getpass.getpass('Пароль ДЕМО-руководителя (от 12 символов): '),getpass.getpass('Пароль ДЕМО-исполнителя (от 12 символов): '))
    print('Созданы manager и inspector. Только демонстрационные данные.')
elif args.command=='run':
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(),host='127.0.0.1',port=args.port,proxy_headers=False)
else:
    target=Path(args.destination)
    if target.exists():parser.error('Файл назначения уже существует')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Reserve destination without ever replacing an existing backup.
    descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(descriptor)
    with sqlite3.connect(args.db) as source,sqlite3.connect(target) as dest:
        source.backup(dest)
        if dest.execute('pragma integrity_check').fetchone()[0]!='ok':raise RuntimeError('Проверка резервной копии не прошла')
        # Do not restore old active browser sessions on rollback.
        dest.execute('delete from sessions')
    print('Резервная копия проверена:',target)
