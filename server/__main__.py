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
run.add_argument('--host',choices=['127.0.0.1','0.0.0.0'],default='127.0.0.1')
backup=sub.add_parser('backup');backup.add_argument('destination');backup.add_argument('--db',default=str(ROOT/'var/ksu.sqlite3'))
restore=sub.add_parser('restore');restore.add_argument('source');restore.add_argument('destination',help='Новый путь БД, существующий файл не заменяется')
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
    trust_local_proxy=os.getenv('KSU_TRUST_LOCAL_PROXY','0')=='1'
    uvicorn.run(create_app(),host=args.host,port=args.port,proxy_headers=trust_local_proxy,forwarded_allow_ips='127.0.0.1',
                limit_concurrency=int(os.getenv('KSU_CONCURRENCY_LIMIT','0')) or None,backlog=64)
else:
    from .maintenance import snapshot_database
    try:
        snapshot_database(args.source if args.command=='restore' else args.db,args.destination)
    except (OSError,ValueError,sqlite3.Error) as error:
        parser.error(str(error))
    print('Проверенная копия готова:',args.destination)
