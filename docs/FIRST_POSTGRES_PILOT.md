# Первый рабочий запуск: чистая PostgreSQL

Владелец подтвердил: приложение не эксплуатировалось, рабочих актов/фото/данных для переноса нет.
**Демо-SQLite в PostgreSQL не переносить.** Сохранить её полный остановленный набор только как
приватный архив. Установку Amvera оставить остановленной до отдельного разрешения на внедрение.
Этот кандидат основан на PR №37; платные ресурсы, merge/deploy и рабочие данные не меняются.

## Что согласовать

- Создание одной managed PG17, бюджет и регион. Ориентир из №37: приложение ~490 ₽ + PG 1 ГБ
  ~490 ₽ + пример storage 2 ГБ ~36 ₽ = около **1 016 ₽/30 дней**; фактическую цену/квоту подтвердить.
- Реальные CA/hostname/RW endpoint, TLS verify-full и совместимость libpq17/pg_dump с Amvera.
  При ALPN/TLS-ошибке не отключать проверку сертификата. Доступ initial owner к CREATE ROLE.
- Приватные имена/логины двух сотрудников, настоящие объекты, назначения inspector, рабочие вопросы.
  Шаблон 22 вопросов — только предложение, требуется утверждение владельца.
- Штатный managed PG backup/restore и внешний носитель, RPO/RTO/ответственный. Ежедневная
  независимая внешняя копия не гарантируется выключенным Windows; одна реплика не даёт HA.

## Порядок владельца

Взять source bundle успешного CI нового Draft PR, сверить SHA/manifest, собрать
`Dockerfile.managed-postgres` в `ksu-managed:rc`. В пакете нет seed.json, БД, пользовательских
паролей или сертификатов. Пример воспроизведения из утверждённого checkout:

```sh
python -m deploy.release_candidate.bundle --profile postgresql --ref <FULL_SHA> --output <NEW_SOURCE_FOLDER>
docker build -f <NEW_SOURCE_FOLDER>/Dockerfile.managed-postgres -t ksu-managed:rc <NEW_SOURCE_FOLDER>
```

1. Сохранить остановленную SQLite/конфигурацию приватно вне Git и облачной синхронизации.
   Ничего из этого архива не загружать в PG, GitHub или Codex. В production-профиле команда
   `migrate` теперь отвергается, `init-demo` и прямой `seed_demo` запрещены.
2. После отдельного разрешения создать новую PG17 и три служебные роли по инструкции №37:
   initial owner отличается от `ksu_admin`, `ksu_runtime`, `ksu_backup`; bootstrap создаёт роли
   без superuser/BYPASSRLS/membership. Runtime получает только свои реквизиты.
   [TLS, private env/CA и bootstrap](POSTGRES_PRODUCTION.md#служебные-права-и-передача-реквизитов).
3. Выполнить создание **пустой схемы**, без пользователей и записей:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation init-schema -EnvFile 'D:\KSU-private\managed-config\admin.env' `
  -OutputDirectory 'D:\KSU-private\schema-<time>' -Image 'ksu-managed:rc'
```

4. Получить шаблон конфигурации (эта операция не подключается к БД):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation template -EnvFile 'D:\KSU-private\managed-config\admin.env' `
  -OutputDirectory 'D:\KSU-private\template-<time>' -Image 'ksu-managed:rc'
```

Скопировать `pilot-template.json` в новый приватный `pilot-config.json`, заполнить локально:
`accounts.manager/inspector.{username,name}`, настоящие `sites`, `inspectorSiteIds`, `questions`.
Пароли в конфигурацию **не** вводить. IDs объектов/вопросов уникальны; минимум один назначенный
объект и 1–200 рабочих вопросов. Начальный порядок вопросов — порядок в массиве. Секции можно
переименовать/добавить. Ни demo/ДЕМО-логинов/записей, ни третьей роли в первичной команде нет.
После проверки каждого вопроса поставить `checklistApproved=true`; пустые значения/false
не принимаются. Можно утвердить предлагаемые 22 пункта либо заменить своим списком.
Не присылать конфигурацию/имена/пароли/CA в чат, GitHub, Actions или Code/Artifacts.

5. Выполнить **однократную первоначальную настройку**:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\Invoke-ManagedPostgres.ps1 `
  -Operation init-production -EnvFile 'D:\KSU-private\managed-config\admin.env' `
  -Source 'D:\KSU-private\pilot-config.json' `
  -OutputDirectory 'D:\KSU-private\initial-setup-<time>' -Image 'ksu-managed:rc'
```

Результаты в новом приватном `production` внутри output: `credentials.json`, `setup-report.json`,
`status.json`. Два пароля генерируются криптографически (192 бита), в БД только salted password
hashes. Имена/логины задаёт владелец. Пароли не возвращаются через stdout, API, audit или образ;
`credentials.json` имеет Linux0600, новый Windows output — owner-only ACL. Сами файлы не шифруются
DPAPI: нужен согласованный защищённый диск/носитель. Передать каждому сотруднику только его
пароль закрытым каналом. Сохранить файл в одобренном хранилище секретов, не в репозитории.

Все бизнес/auth таблицы проверяются под транзакционной блокировкой; существующие данные не
меняются и не объединяются. Создаются только пользователи, объекты, назначения, вопросы,
версия чек-листа и аудит настройки. Актов/фото/нарушений/инвентаризаций/выездов нет.
Повторный запуск на непустой БД запрещён, режима force/upsert/reset нет. SQL-схема не ограничивает
количество сотрудников двумя — это только объём данной команды первоначальной настройки.

Пароли сохраняются **до commit**, чтобы не потерять их при сбое сети. Успех подтверждают
`status.phase=committed` и `setup-report.initialized=true`. При ошибке/неполном копировании
сохранить private Docker volume/контейнер: credentials могли уже сохраниться, а commit мог пройти.
Не повторять инициализацию, не очищать БД и не генерировать новые пароли поверх существующих.
Владелец приватно сверяет состояние/извлекает сохранённые файлы; stdout не содержит секретов.

6. До входов приложения выполнить `Operation verify` ролью backup с `Source` на
   `production\setup-report.json`, новым output, как в №37. Затем `Operation backup` и
   `Test-ManagedPostgresBackup.ps1`: новая локальная PG, все 16 fingerprints, constraints,
   grants и пустые sessions. [Команды backup/independent restore](POSTGRES_PRODUCTION.md#backup-и-восстановление-в-эксплуатации).
   Проверенные рабочие записи и пароли не отправлять в CI/Codex; CI использует только
   временные синтетические аккаунты, их credential-файлы не попадают в artifacts/логи.
7. После отдельного go/no-go ручная доставка в **существующее** приложение. Убрать старые
   автосвязи GitHub→Amvera до merge, `KSU_DB`/bootstrap/admin/backup secrets; передать только
   runtime DSN с verify-full. UID10001, одна реплика, прежний origin, новый постоянный production
   deployment ID. `KSU_OFFLINE_MODE=off`, `KSU_VIEWER_ACCESS=off`, `KSU_LOCAL_BACKUP_MODE=off`.
   Для будущего pilot список inspector IDs брать из **приватного setup-report**, не писать
   литерал `inspector`: IDs сотрудников теперь случайные и неизменные.
8. Короткая проверка: health SHA/backend, вход двух сотрудников их новыми паролями, настоящие
   объекты/назначения и вопросы, отсутствие демо-подписей. В «Настройки» manager проверяет
   рабочий шаблон. Полную UI/PWA-приёмку не повторять. После этого разрешить записи и согласованный
   offline pilot. Viewer не создаётся автоматически. Архив SQLite сохраняется отдельно.

Эксплуатационные backup/расходы/TLS из №37 сохраняются. Откат до новых записей — остановить
новый сервис и оставить архив нетронутым; SQLite WAL на Amvera не возобновлять. После новых
PG commits — совместимое исправление или восстановление в новую PG со сверкой дельты,
не замена старой демо-базой. Runtime не выполняет первоначальную настройку при старте.

[Редактор чек-листа](CHECKLIST_MANAGEMENT.md) · [Будущие пользователи](FUTURE_USER_ADMIN.md).
