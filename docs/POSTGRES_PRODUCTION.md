> **Транспорт Amvera обновлён после ответа поддержки:** [профили, ограничения и первый запуск](AMVERA_PG_TRANSPORT.md). Прежнее требование verify-full/CA ниже относится к другим провайдерам; для Amvera использовать только явный профиль из новой инструкции. Это не сквозной TLS.

> **Уточнение владельца:** рабочих данных для переноса нет. Для текущего пилота использовать [FIRST_POSTGRES_PILOT.md](FIRST_POSTGRES_PILOT.md); SQLite только архивировать. Разделы миграции ниже сохранены как историческая инструкция, не как шаг запуска.

# Рабочий кандидат на managed PostgreSQL Amvera

Основание перехода: официальный ответ поддержки, переданный владельцем 10.11.2026 — SQLite
WAL на постоянном `/data` не поддерживается. Приложение остановлено. Выпуск SQLite PR №36
заблокирован; нельзя включать его daily SQLite backup или запускать старый runtime на этом томе.
Новый кандидат использует существующие PG backend/RLS/миграции/фото/receipts/отчёты, не новый UI.
[Аудит кода и границ](POSTGRES_PRODUCTION_AUDIT.md). Полная UI/PWA-приёмка №35 не повторяется.

## Архитектура и стоимость для согласования

Один существующий сервис приложения + **один managed PostgreSQL 17**, одна реплика в согласованном
регионе РФ (ориентир Москва_0). Приложение и owner tools используют TLS `verify-full` с явным
CA-файлом и проверкой имени. Фото/миниатюры, пользователи/хеши паролей, акты, история, импорты,
receipts хранятся в PG. На `/data` остаётся сохранённый исходный набор SQLite и при необходимости
публичный CA; новая версия не открывает там SQLite и не делает туда database backup/locks.
Временная обработка — `/tmp/ksu-managed-work`, контейнерная локальная FS, не NFS. Минимум 256 МиБ
свободного scratch; в тесте tmpfs 768 МиБ (занимает RAM по факту, не заранее) при лимите 1 ГБ.

Публичные ориентиры проверены 10.11.2026, не оферта/заказ:

| Состав | Оценка за 30 дней |
| --- | ---: |
| Существующее приложение 0,5 CPU / 1 ГБ, если тариф «Начальный Плюс» | около 490 ₽ |
| Одна PG-реплика «Начальный Плюс», 0,5 CPU / 1 ГБ | около 490 ₽ |
| Пример managed storage 2 ГБ × 18 ₽/ГБ | около 36 ₽ |
| Итого ориентир работающего контура | **около 1 016 ₽**, прирост к приложению около 526 ₽ |

Источники: [managed PG](https://amvera.ru/managedpostgresql), [общие тарифы](https://docs.amvera.ru/general/price.html).
У managed-страницы отдельная цена storage, общая документация показывает SSD-квоты; итоговую
модель/минимальный размер/регион/НДС/сетевые расходы подтвердить в калькуляторе **до заказа**.
Почасовые 0,68 × 720 дают 489,60 ₽ за компонент, календарные месяцы отличаются от 720ч.
Минимальный PG «Начальный» около 290 ₽/0,5 ГБ существует, но этот меньший ресурс не является
проверенным production-профилем. Две реплики увеличат compute, не заказываются автоматически.
Прежний бюджет 500 ₽/30 дней не покрывает предложенный контур — нужен новый бюджет владельца.

Caps приложения не повышены: PG database admission 256 МиБ, dump package 512 МиБ, scratch
2 ГиБ. Это не лимит счёта провайдера: PG WAL, индексы, backup/служебные файлы и Artifacts
считаются отдельно. При миграции более крупного фактического объёма остановиться и согласовать
вместимость; квоты/тарифы не расширяются автоматически. Сервис остановлен сейчас, фактические
текущие начисления и правила длительной остановки проверить приватно в кабинете.

Никакие managed PG, registry, CronJob, VM, pgAdmin-сервис или дополнительные платные ресурсы
этой работой не создаются. Временные PostgreSQL в CI/Windows Docker не являются хостингом.

## Служебные права и передача реквизитов

| Роль | Где применяется | Права |
| --- | --- | --- |
| Владелец/initial provider admin | Только bootstrap новой пустой БД | Создать три роли, выдать CONNECT/CREATE admin, закрыть PUBLIC CREATE/CONNECT в этой новой БД |
| `ksu_admin` | Только отдельный owner-local контейнер миграции/restore | Владелец `ksu`, COPY/DDL и CREATE schema в выделенной БД; не superuser, без CREATEROLE/CREATEDB/BYPASSRLS |
| `ksu_runtime` | Только секрет существующего приложения | Не владелец, без DDL/membership/BYPASSRLS; grants и RLS прежнего backend |
| `ksu_backup` | Только owner-local backup | SELECT/sequence-read и явные read-only RLS policies; без BYPASSRLS/записи/DDL/membership |

Приложению не передавать admin/backup/bootstrap DSN или их пароли: entrypoint это отвергнет.
Backup не требует manager-пароля/HTTP-доступа, выполняется отдельно. HTTP `/api/admin/backup`
без backup-секрета остаётся не настроен (503 для manager, запрет для viewer/inspector).
Это сознательное разделение операционного backup и runtime. Прежняя Windows SQLite-job не
подходит для PG: сохранить её профили/копии, отключить точные старые backup+health задачи.

Реальные значения вводит владелец в кабинете/одобренном хранилище секретов и приватных локальных
файлах с owner-only ACL на защищённом диске. Не присылать их в чат, Codex, issue, PR, Actions
secrets, логи, `.env` репозитория, Code/Artifacts или облачную синхронизацию. Нужны отдельные
пароли минимум 20 случайных символов; пароли пользователей приложения из SQLite не меняются.

Для owner tools создать приватный каталог вне Git, например `D:\KSU-private\managed-config`:
`pg-ca.pem` и отдельные `bootstrap.env`, `admin.env`, `backup.env`. В каждом только нужный DSN:

```text
# Пример структуры admin.env, реальные значения подставляются ПРИВАТНО владельцем:
KSU_PG_ADMIN_DSN=host=<verified-owner-endpoint> port=5432 dbname=<new-database> user=ksu_admin password=<private-password> sslmode=verify-full sslrootcert=/config/pg-ca.pem
```

Для bootstrap — `KSU_PG_BOOTSTRAP_DSN`, для backup/verify — `KSU_PG_BACKUP_DSN`. Если пароль
имеет пробел/кавычку, использовать корректное libpq escaping; удобнее сгенерировать случайный
base64 пароль без пробелов. Отдельный приватный `service-passwords.json` содержит ключи
`ksu_admin`, `ksu_runtime`, `ksu_backup`; bootstrap читает его через stdin, не argv. Сначала
ограничить ACL каталога; DPAPI старой SQLite-job **не шифрует эти новые env/backup-файлы**.
Владельцу согласовать шифрование диска, хранение и удаление локальных административных секретов.

## До создания БД: разрешение и проверки Amvera

1. Утвердить бюджет/регион/одну реплику/PG17, RPO/RTO, ответственного, внешний backup и окно.
   **Отдельное разрешение на создание платной PG ещё необходимо.** Код не заказывает её.
2. У провайдера подтвердить: PG17 доступен; initial owner может CREATE ROLE (либо временный
   Superuser Access только для bootstrap); доступен CA и hostname с правильным SAN для
   `verify-full`; схема подключения app/owner и `pg_dump` поддержана. Предпочтителен внутренний
   RW endpoint для приложения; внешний тип POSTGRES нужен owner-local переносу/backup.
3. [Документация PG](https://docs.amvera.ru/databases/postgreSQL.html) описывает внутренние
   `...-rw`/`-ro`/`-r` и предупреждение о libpq17/ALPN на внешнем endpoint. Для экспортируемого
   snapshot использовать RW-primary и тот же endpoint для psycopg и pg_dump; не случайную
   реплику/transaction pooler. CA/имя для внутреннего и внешнего подключения могут различаться.
   Не копировать пример `sslmode=prefer` из документации: кандидат требует `verify-full`.
   Если shipped clients17 не подключаются, остановиться и решить с поддержкой; не отключать TLS.
4. Подтвердить штатный **managed PG backup**: включение, ежедневность, хранение/восстановление.
   Публичная managed-страница заявляет семь дней на всех планах. Ежедневный `/data` backup
   существующего приложения — другой механизм и не копия внешней PG. PITR/точный RTO не
   считать доступными без подтверждения в выбранном сервисе.

## Доставка и owner-local операции

Взять source artifact успешного CI нового Draft PR, сверить full SHA и manifest. В нём
`profile=postgresql`, `Dockerfile.managed-postgres`, managed entrypoint, зависимости, принятые
UI/assets и generic SQL. Нет БД, seed.json, ключей, сертификатов, паролей, backup или .git.
Пакет можно повторить из утверждённого checkout:

```sh
python -m deploy.release_candidate.bundle --profile postgresql --ref <FULL_SHA> --output <NEW_SOURCE_DIRECTORY>
python -m deploy.release_candidate.bundle --verify <NEW_SOURCE_DIRECTORY>
docker build -f <NEW_SOURCE_DIRECTORY>/Dockerfile.managed-postgres -t ksu-managed:rc <NEW_SOURCE_DIRECTORY>
```

Windows без Python: скачать готовый CI-пакет, сверить `source_sha` и SHA-256 файлов manifest
через `Get-FileHash`, затем `docker build` как выше. Bundle-only amvera.yml выбирает managed
Dockerfile, существующий `/data`, порт 8000; SHA встроен в Dockerfile без build arguments.
Корневой amvera.yml старой установки не переключается автоматически. Образ также служит
owner CLI, но не запускает PostgreSQL daemon. Все операции ниже с preloaded image, `--pull never`.

`Invoke-ManagedPostgres.ps1` работает на компьютере владельца. Требует новый output вне Git,
закрытый input, private env/CA и Docker Desktop/Linux. Scratch/output в новом Linux volume,
не SQLite WAL на NTFS/SMB. Ни Amvera API, ни автоматического deploy/загрузки в GitHub нет.
Исходный файл read-only, SHA сверяется после операции; существующие результаты не заменяются.
Контейнер/volume остаются для разбора, удалять их отдельно по точным именам после проверки.

После разрешения владелец **сам** создаёт managed PG в кабинете: PostgreSQL → новая БД,
PG17, выбранный тариф/одна реплика, отдельное имя БД и initial owner. Не использовать
зарезервированное `postgres` как имя новой рабочей БД. Настроить подтверждённые endpoints/CA,
закрыть лишний доступ. Образ/bootstrap сначала проверяет TLS, затем создаёт роли в пустой БД:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation bootstrap -EnvFile 'D:\KSU-private\managed-config\bootstrap.env' `
  -Source 'D:\KSU-private\managed-config\service-passwords.json' `
  -OutputDirectory 'D:\KSU-private\pg-bootstrap-<time>' -Image 'ksu-managed:rc'
```

Это меняет только **новую разрешённую БД/роли**, не существующую SQLite. При коллизии ролей/
непустой БД — отказ, не переиспользовать наугад и не удалять существующие схемы.

## Перенос остановленной SQLite

1. Убедиться, что прежнее приложение, backup-процессы и иные SQLite-писатели остаются
   остановленными. Активных пользователей сейчас нет; не возобновлять их на исходной базе
   во время подготовки копии. Сохранить приватно текущий образ/SHA и конфигурацию.
2. Скачать **полный frozen `/data`** в новый защищённый каталог владельца, включая main,
   WAL/SHM/journal при наличии. Не открывать SQLite/делать checkpoint на Amvera/NFS. Получить
   независимый standalone snapshot через существующий `New-Offline-Backup.ps1` на Linux-
   volume, указав фактический `-DatabaseRelativePath`. Input frozen read-only. Скрипту явно
   передать `-Image ksu-managed:rc`. Это отдельная копия, не правка original `/data`.
3. Дождаться integrity/FK и сохранности хешей frozen-набора. Если исходный набор повреждён,
   остановиться; остановка сама по себе не исправляет прежние WAL-повреждения. Проверить
   доступную provider-копию на новом локальном пути и согласовать точку восстановления.
4. Перенести **независимый закрытый backup** в новую PG:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation migrate -EnvFile 'D:\KSU-private\managed-config\admin.env' `
  -Source 'D:\KSU-private\independent-<time>\ksu-backup.sqlite3' `
  -OutputDirectory 'D:\KSU-private\pg-migration-<time>' -Image 'ksu-managed:rc'
```

CLI узнаёт известные SQLite схемы 0/1/2/3/4, сначала на новой приватной копии приводит к v4,
создаёт известную PG-схему и COPY в пустой target одной транзакцией данных. Сверяет fingerprints
всех 16 таблиц (включая BLOB/хеши паролей/audit/receipts), FK, audit sequence, исходный SHA.
Неизвестная/частичная схема, непустой PG target, превышение cap или несовпадение — отказ.
DDL initialization может оставить **новую** пустую схему после неудачи; не повторять импорт
поверх неё и не удалять автоматически. Владелец разбирает/создаёт новый target после решения.

`migration.json` приватный, `source_unchanged=true`, `schema_after=4`, fingerprints и sessions_revoked.
Сессии/попытки очищены только в копии, пользователи и пароли сохранены. Viewer автоматически
не создаётся. CLI возвращает `ready_for_writes=false`: перенос ещё не разрешает работу.
Для Linux volume запас: миграция минимум 4×SQLite +128 МиБ, dump/пакет до 2×512 МиБ+64 МиБ,
restore ещё dump/PG volume. Не считать Windows exports и Docker volume одним экземпляром места.

5. До первого входа приложения сверить target с приватным отчётом и проверить role/TLS:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation verify -EnvFile 'D:\KSU-private\managed-config\backup.env' `
  -Source 'D:\KSU-private\pg-migration-<time>\migration.json' `
  -OutputDirectory 'D:\KSU-private\pg-verify-<time>' -Image 'ksu-managed:rc'
```

6. Получить новую PG-копию и **независимо восстановить** её на локальном Docker:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation backup -EnvFile 'D:\KSU-private\managed-config\backup.env' `
  -OutputDirectory 'D:\KSU-private\pg-backup-<time>' -Image 'ksu-managed:rc'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Test-ManagedPostgresBackup.ps1 `
  -Backup 'D:\KSU-private\pg-backup-<time>\backup.ksubackup' `
  -OutputDirectory 'D:\KSU-private\pg-restore-test-<time>' -Image 'ksu-managed:rc'
```

Нужен также заранее загруженный pinned postgres:17 image из скрипта. Проверка создаёт только
новый локальный PG/volumes/временные локальные пароли и TLS, не публикует порты и не требует
admin-реквизитов облачной БД. Restore доверенного SQL-пакета выполняется в пустую БД; проверяются
manifest/dump SHA, все 16 fingerprints, constraints, пустые sessions/attempts, grants. На успехе
новый сервер останавливается, backup/отчёт/volumes сохраняются. При ошибке сохранены ресурсы
для приватного разбора; работа не разрешается. Нельзя подставлять недоверенный .ksubackup:
pg_restore исполняет SQL. Backup содержит полные данные/хеши паролей и требует защиты диска.

## Подключение существующего приложения после отдельного разрешения

До любого будущего merge/ручной доставки отключить старые внешние GitHub→Amvera hooks/watch и
прежние deploy/backup Actions в main. Их заглушки в ветке сами main/настройки провайдера не меняют.
Новый source bundle доставляется владельцем только в существующий app-сервис, без БД/секретов
в Code/Artifacts. Старый SQLite-runtime остаётся остановлен. Для новой версии:

```text
KSU_DATABASE_URL=<runtime-only DSN, verified RW host, ksu_runtime, verify-full, correct CA path>
KSU_ORIGIN=https://<same-production-host>
KSU_OFFLINE_MODE=off
KSU_OFFLINE_DEPLOYMENT_ID=<stable-production-32-hex-ID>
KSU_OFFLINE_USER_IDS=<approved inspector IDs or empty while off>
KSU_VIEWER_ACCESS=off
KSU_LOCAL_BACKUP_MODE=off
```

URL вводится как секрет платформы; альтернативно один `KSU_DATABASE_URL_FILE` с приватным
файлом, но не оба. CA-файл доступен UID10001, например `/data/pg-ca.pem` или штатный секретный
mount. Не загружать локальные test TLS-ключи; внешний HTTPS остаётся у ingress Amvera.
Убрать KSU_DB, admin/backup/bootstrap DSNs/passwords и неявные PGHOST/PGPASSWORD/PGSERVICE.
Новый entrypoint откажет при SQLite-настройках или лишних служебных реквизитах, не создаст
пустую БД и не мигрирует на старте. UID/GID10001, одна реплика/worker, caps сохранены.

Для уже существующего рабочего offline-контура сохранить production deployment ID и origin;
если это первое рабочее включение — создать ID один раз, отличный от синтетики/8020. Старые
пакеты/receipts привязаны к данным/идентичности; смена ID/очистка браузерной очереди не являются
способом миграции или отката. Не брать ID/учётные записи из приёмочного стенда.

После ручной сборки health через штатный HTTPS: `build=<approved SHA>`, `backend=postgresql`,
`data=application`, `offline=off`. Короткий smoke **переноса данных** у владельца: вход прежними
manager/inspector, выбранные старые объект/акт/фото/thumbnail, PDF/XLSX, история и receipts.
Не создавать демонстрационные данные в production; полную UI/PWA-приёмку не повторять.
Включить viewer после проверки персональных site ACL, затем online; offline pilot — только
по согласованным inspector IDs. Отметить время первой новой бизнес-записи и снять freeze.

## Backup и восстановление в эксплуатации

- Основной ежедневный автоматический слой — **штатные согласованные managed PG backups**,
  не backup файлов `/data`. Владелец проверяет наличие свежей успешной точки каждый день,
  хранение и хотя бы одну репетицию provider restore на новом target без замены работающей БД.
- Независимый слой — owner-local `backup` → `Test-ManagedPostgresBackup.ps1`, перед обновлением
  и по согласованному расписанию при доступности Windows/Docker. Source-роль только SELECT;
  dump использует consistent exported snapshot, одновременно приложение останавливать не нужно.
  `--enable-row-security` необходим для NOBYPASSRLS, полные read policies проверяет preflight.
- Успех pg_dump, SHA или зелёный health приложения не заменяют restore. Считать копию проверенной
  только вместе с соответствующим `restore.json` (verified=true, совпадающий package_sha256),
  всеми таблицами и временем фактической проверки. Старые файлы не перезаписываются/не удаляются.
- Простой исходный retention для согласования: две последние проверенные независимые копии на
  защищённом внешнем носителе; старую удалять вручную лишь после успеха новой и сверки SHA/restore.
  Локальные recovery volumes тоже занимают место. Не делать prune и не расширять платный диск.
- Всегда включённого внешнего узла сейчас нет/он неизвестен. Поэтому автоматическая ежедневность
  обещается только подтверждённому provider-слою; **ежедневная независимая внешняя копия не
  гарантирована**. Для потери аккаунта/провайдера RPO ограничен последней Windows/носитель-копией.
  До важных данных владелец принимает этот риск либо даёт уже имеющийся корпоративный узел для
  тех же owner tools; новый платный ресурс не создаётся. Одной реплики недостаточно для HA.
- Целевой RPO до суток для provider-сбоя — лишь после подтверждения расписания/доступности
  копий. RTO измерить владельцем на своей БД/сети; время CI не является обещанием. PITR и
  восстановление на произвольную секунду не заявляются. Повторять independent restore после
  каждого backup, первый smoke ролей/фото — перед выпуском, не ежедневно повторять UI-приёмку.

Контроль: свежесть provider/independent backup+restore, cap/физический PG/WAL/storage, временный
диск/память приложения, DB connections/long transactions, ошибки503/507 и offline conflicts.
Автоматического внешнего мониторинга или отправки писем/паролей здесь нет; ответственный назначается.

## Откат

До первой новой бизнес-записи в PG: остановить новый runtime, сохранить PG/отчёты/backup,
вернуть старый образ/конфигурацию **в остановленное состояние** с нетронутой SQLite. Это отмена
перехода, а не разрешение снова запускать WAL на несовместимом `/data`. Для продолжения работы
нужно исправление PG-кандидата либо отдельно согласованное безопасное хранилище/режим старого контура.
В рамках этого плана SQLite на Amvera не возобновляется даже до новых записей.

После новых PG commits старую SQLite вернуть нельзя: потеряются записи/история/receipts.
Закрыть запись, отключить offline/viewer на той же PG и том же deployment ID, сделать свежий
PG backup/restore, исправлять совместимый v4 PG-код либо восстанавливать на новом PG target с
согласованной сверкой дельты. Не перетирать production restore-командой, не удалять receipts,
не переключать две базы на запись одновременно. Обратной автоматической миграции PG→SQLite нет.

Оставшиеся ворота: новый бюджет/создание БД, действительный TLS/CA/ALPN/PG17/права у Amvera,
owner-local backup/migration/restore реальной остановленной базы, provider restore, RPO/RTO,
защита/retention внешних копий и ручное go/no-go внедрения. CI не проверяет существующую Amvera.
[Короткий чек-лист](OWNER_POSTGRES_CHECKLIST.md).
