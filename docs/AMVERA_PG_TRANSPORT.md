# PostgreSQL Amvera: транспорт и первый запуск поверх PR №38

Основа: `release/clean-postgres-pilot`, SHA `87373094b6b84acd56f2a7dc7627d73f84716562`.
UI, объекты, структура/формулировки 22 вопросов, роли сотрудников, offline и бизнес-логика
не меняются. Здесь нет создания ресурсов, подключения к рабочей PG, merge или deploy.

## Что подтверждено и где проходит TLS

Владелец передал ответ поддержки: TLS завершается на входящем прокси Amvera, следующий
участок до PostgreSQL не шифруется. Внешний verify-full не проходит проверку сертификата;
require достигает авторизации (это ещё не подтверждение корректного пароля/прав/схемы).

[Сетевые правила Amvera](https://docs.amvera.ru/applications/configuration/network.html)
описывают внутренний сервис `cnpg-<проект>-rw`, необходимость одного региона и отсутствие
шифрования внутренней сети со стороны провайдера.
[Пример Amvera](https://docs.amvera.ru/general/examples/go-postgresql.html) использует
sslmode=disable внутри сети. Документация [PostgreSQL Amvera](https://docs.amvera.ru/databases/postgreSQL.html)
показывает внешний prefer и предупреждает об ALPN/libpq17. **Prefer в этом кандидате запрещён**;
совместимость настоящего endpoint должен подтвердить владелец. Код не переключает режим при ошибке.

| Явный профиль `KSU_PG_TRANSPORT_PROFILE` | DSN | Гарантии/границы |
| --- | --- | --- |
| `verify-full` (по умолчанию) | Remote: verify-full; managed: явный CA и один host | CA/hostname; прежний локальный dev/restore-контур сохранён |
| `amvera-external` | Только require, один закреплённый DNS host, port=5432 | TLS от клиента до прокси; нет гарантии подлинности сервера или шифрования последующего участка |
| `amvera-internal` | Только disable, host=cnpg-instrakt-ksu-rw, port=5432 | Открытый текст только по явно выбранному частному маршруту; TLS не предполагается |

Профиль не берётся из DSN и не угадывается по домену. Для external дополнительно обязателен
`KSU_AMVERA_PG_EXTERNAL_HOST`: точный внешний FQDN из кабинета, без схемы/порта/списка/IP.
Для internal обязательно `KSU_AMVERA_INTERNAL_NETWORK=confirmed-private`.
Другие хосты, hostaddr в исходном DSN, multihost, Unix socket, неявные sslmode/port,
смешанные PG* параметры окружения и неподдерживаемые DSN extensions отвергаются.
Native pg_dump/pg_restore получают ту же политику, без паролей в argv.

Internal разрешается только для точного service name, если **все** DNS-ответы — IPv4 RFC1918
(10/8, 172.16/12, 192.168/16). Проверенный IP закрепляется в hostaddr libpq, проверяется
фактический адрес соединения; публичные/loopback/link-local/IPv6-ответы и DNS-ошибка закрывают
подключение. Отказ не включает внешний маршрут или TLS автоматически. Это защита конфигурации,
**не доказательство размещения в Amvera или изоляции арендаторов**: её обеспечивает провайдер.
Владелец должен подтвердить один регион, внутреннюю доступность и сетевые ограничения в кабинете.
На компьютере владельца internal не использовать, не имитировать DNS/hosts и не туннелировать
plaintext наружу. Для внешних owner-операций — external require.

Риск external: без CA/hostname verification возможны подмена endpoint/MITM и кража пароля/данных.
Риск internal: перехват/подмена в доступной внутренней сети, включая пароли/фотографии/backup.
Уникальные служебные пароли и минимальные SQL-права уменьшают последствия, но не заменяют
проверку сервера/шифрование. Если эти риски неприемлемы, внедрение остановить до поддержанного
провайдером verify-full или другого согласованного решения; не включать prefer/allow.

## Сначала read-only probe, затем права и чистая настройка

Все действия ниже выполняет владелец **после отдельного согласования**. Рабочую PG этой задачей
не проверяли и не изменяли. Не пересоздавать существующую БД; при обнаружении схемы/данных
остановить инициализацию и выяснить их происхождение, не удалять и не применять force/reset.

1. Взять source bundle успешного CI этого Draft PR, сверить полный SHA/manifest и собрать
   `Dockerfile.managed-postgres` в ksu-managed:rc. Скрипты tools взять из checkout того же SHA.
   Новых платных ресурсов не требуется; использовать только ранее разрешённую PG.
2. Локально вне Git/синхронизации подготовить приватные env-файлы. Пароли/DSN/CA/backup
   не присылать в чат, GitHub или CI. Не использовать переменные PGHOST/PGSSLMODE/PGPASSWORD:
   настройки задаются внутри соответствующего KSU_* DSN. Пример **без настоящего секрета**:

```text
KSU_PG_TRANSPORT_PROFILE=amvera-external
KSU_AMVERA_PG_EXTERNAL_HOST=<точный-внешний-домен-PG>
KSU_PG_BOOTSTRAP_DSN=host=<тот-же-домен> port=5432 dbname=<разрешённая-БД> user=<initial-owner> password=<PRIVATE_SECRET> sslmode=require
```

Для admin/backup/runtime — отдельные файлы с теми же двумя profile/host строками и соответственно
KSU_PG_ADMIN_DSN (ksu_admin), KSU_PG_BACKUP_DSN (ksu_backup), KSU_DATABASE_URL (ksu_runtime).
Файлы *_FILE поддерживаются прежним secret binding. CA/sslrootcert не задавать external-профилю;
обычный verify-full для других провайдеров сохраняет прежнюю обязательную CA-проверку.

3. Выполнить read-only проверку initial owner (не создаёт роли/схему/записи):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation probe -Role bootstrap -EnvFile 'D:\KSU-private\managed-config\bootstrap.env' `
  -OutputDirectory 'D:\KSU-private\transport-probe-<time>' -Image 'ksu-managed:rc'
```

Успех external: connected=true, profile=amvera-external, client_tls=true,
server_identity_verified=false, end_to_end_tls_claimed=false. postgres_tls показывает только
последний участок PostgreSQL; при описанной поддержкой схеме ожидается false. `tls_verified`
для обоих Amvera-профилей всегда false. Ошибка не выводит DSN/пароль/сырой libpq exception.
При password authentication failed исправлять реквизиты/права приватно, не снижать sslmode.
При ALPN/SSL error остановиться и сверить endpoint/клиент с поддержкой; скрипт использует
PostgreSQL SSLRequest negotiation и SNI. Успех require не доказывает подлинность proxy.

4. Если новая разрешённая PG ещё не подготовлена: выполнить bootstrap трёх служебных ролей,
   затем init-schema, template и init-production по [чистому запуску](FIRST_POSTGRES_PILOT.md).
   Использовать новые external env для **каждой** операции; не применять прежние Amvera DSN
   verify-full/CA и не мигрировать демо-SQLite. Если роли/схема/данные уже существуют, не
   повторять bootstrap/init-schema/init-production. На каждом этапе отказ означает остановку.
   В production ничего не seed-ится при старте, повторная настройка непустой БД запрещена.
5. Проверить каждую служебную роль через Operation preflight -Role admin/runtime/backup с
   её собственным env-файлом и новым output. Schema owner только ksu_admin; runtime/backup —
   NOSUPERUSER NOBYPASSRLS NOINHERIT, без DDL/membership. Backup только SELECT и полные RLS
   read policies. Приложение получает **только runtime**, admin/backup/initial-owner вне app.
6. До первого входа verify нового setup-report, затем backup ролью ksu_backup и независимый
   Test-ManagedPostgresBackup.ps1 из инструкции №38. Source использует external require;
   независимый локальный restore намеренно задаёт **verify-full** с временным локальным CA.
   Проверить все 16 fingerprints, constraints/grants и пустые sessions, сохранить приватные
   отчёты. После первых входов init setup-report уже не заменяет свежую проверенную backup-копию.

## Runtime существующего приложения

Без отдельного разрешения существующее приложение не запускать/не обновлять. Для первого
согласованного запуска можно оставить **external require** и проверить health SHA/backend,
вход двух сотрудников, права и свежий backup, без повторной UI/PWA-приёмки.

Internal — отдельный выбор после подтверждения частной сети. В разрешённой среде существующего
приложения задать только runtime secret и выполнить read-only команду до допуска записей:

```text
KSU_PG_TRANSPORT_PROFILE=amvera-internal
KSU_AMVERA_INTERNAL_NETWORK=confirmed-private
KSU_DATABASE_URL=host=cnpg-instrakt-ksu-rw port=5432 dbname=<разрешённая-БД> user=ksu_runtime password=<PRIVATE_SECRET> sslmode=disable
```

```sh
python -m server.managed_pg probe --role runtime
python -m server.managed_pg preflight --role runtime
```

Ожидать client_tls=false, postgres_tls=false, server_identity_verified=false. Команды требуют
реальной внутренней DNS/маршрутизации; снаружи они должны отказать. Если внутренней среды для
проверки нет, использовать проверенный external-профиль, не объявлять internal проверенным.
Остальные runtime flags, origin, deployment ID, viewer/offline gates и private users/config
из №38 не менять. Browser HTTPS и транспорт PostgreSQL — разные соединения.

Штатный provider backup и его restore, внешняя копия/retention/RPO/RTO остаются по плану №37–38.
Выключенный Windows всё ещё не гарантирует ежедневную независимую копию. При ошибке транспорта
приложение не стартует, SQLite fallback не выполняется. Откат до новых записей — остановка и
возврат согласованной конфигурации/образа; не возвращать режим verify-full, уже не работающий
на Amvera, не возобновлять SQLite WAL. После новых записей — только совместимое восстановление
с сохранением актуальных данных/receipts. Merge/deploy этой задачей не разрешены.
