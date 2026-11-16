# PR46 — команды владельцу

Порядок/условия: [RELEASE_GO_NO_GO.md](RELEASE_GO_NO_GO.md).
Ниже отдельно обозначены **чтение сейчас**, локальная подготовка и **внедрение после GO**.
Не вставлять в PowerShell нумерованные инструкции или весь Markdown.
Пароли/DSN не вводить в команды, чат или GitHub; использовать свои существующие приватные env.

## Проверка существующего приложения и БД

**Сейчас, PowerShell на ПК:** только GET по фактическому HTTPS-адресу приложения из кабинета.
Доверие TLS не отключается.

```powershell
$Origin = (Read-Host 'HTTPS адрес существующего приложения без завершающего /').TrimEnd('/')
$address = [Uri]$Origin
if ($address.Scheme -ne 'https' -or $address.UserInfo -or $address.AbsolutePath -ne '/') { throw 'Нужен точный HTTPS origin без логина/пароля и пути' }
Invoke-RestMethod -Uri ($Origin + '/api/health') -TimeoutSec 20 | Select-Object status,build,backend,offline
```

**Сейчас, существующая консоль приложения Amvera (Linux shell, не PowerShell):** если эти
модули уже есть в работающем образе. Только preflight чтения; не устанавливать пакеты.
Вывод ошибки санитарный, без DSN. Проверка не выполняет DDL/seed/миграцию.

```sh
python -m server.managed_pg probe --role runtime
python -m server.managed_pg preflight --role runtime
```

Если консоли/модуля нет, отметить это и использовать уже настроенное приватное соединение
в DBeaver/pgAdmin. **SQL editor, не PowerShell:** исполнить под фактической runtime-ролью.
Перед этим приватно сверить host/dbname с настройкой работающего приложения; без такой сверки
успех в другой PG ничего не подтверждает. Транзакция только для чтения, завершается rollback.

```sql
BEGIN READ ONLY;
SELECT current_user AS runtime_role,
       current_setting('server_version') AS pg_version,
       (SELECT max(version) FROM ksu.schema_version) AS schema_version,
       pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT pg_get_userbyid(nspowner) AS schema_owner
FROM pg_namespace WHERE nspname='ksu';
SELECT rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid) AS membership
FROM pg_roles r WHERE rolname=current_user;
SELECT to_regprocedure('ksu.viewer_read_version()') AS viewer_marker,
       to_regprocedure('ksu.inventory_count_version()') AS inventory_marker;
SELECT count(*) AS rls_tables FROM pg_class
WHERE relnamespace='ksu'::regnamespace AND relkind='r' AND relrowsecurity;
SELECT count(*) AS viewer_restrictions FROM pg_policy
WHERE polrelid IN (SELECT oid FROM pg_class WHERE relnamespace='ksu'::regnamespace)
  AND polname='viewer_restriction' AND NOT polpermissive;
ROLLBACK;
```

Ожидается `ksu_runtime`, PG17, schema4, owner `ksu_admin`, все булевы role properties false,
15 RLS tables / 15 viewer restrictions. Только **если соответствующий marker найден**:

```sql
BEGIN READ ONLY;
SELECT ksu.viewer_read_version();
ROLLBACK;
```

Ожидается 2. Если inventory flag в production `on`, marker должен быть и равняться 1:

```sql
BEGIN READ ONLY;
SELECT ksu.inventory_count_version(),
       has_function_privilege(current_user,'ksu.inventory_count_version()','EXECUTE');
ROLLBACK;
```

Нет marker/доступа — STOP, не запускать миграцию. Размер PG из SQL не равен свободному
диску: запас/реплики/backup проверяются в метриках и настройках текущего ресурса в кабинете.

## Локальная подготовка принятого пакета

Docker Desktop Linux containers и Git for Windows. Из **зелёного CI согласованного полного
SHA** скачать artifacts `PR46-Production-Source` и `PR46-Owner-Tools`. Распаковать соответственно
в новые папки `C:\KSU\PR46-Production-Source` и `C:\KSU\PR46-Owner-Tools`.
Второй содержит `tools` и `docs`; это обычные существующие owner-скрипты, не тестовый seed.
Полный SHA для сверки — из итогового отчёта Codex и страницы этого CI, не из имени ветки.
Сборка ниже происходит только на ПК, не запускает приложение или PostgreSQL.

```powershell
$ErrorActionPreference = 'Stop'
$Source = 'C:\KSU\PR46-Production-Source'
$OwnerTools = 'C:\KSU\PR46-Owner-Tools'
$ExpectedSha = (Read-Host 'Полный SHA из согласованного отчета Codex и зеленого CI').Trim()
$manifest = Get-Content -Raw -LiteralPath (Join-Path $Source 'release-manifest.json') | ConvertFrom-Json
if ($ExpectedSha -cnotmatch '^[a-f0-9]{40}$' -or $manifest.source_sha -cne $ExpectedSha -or $manifest.profile -ne 'postgresql' -or $manifest.format -ne 'ksu-source-delivery-v1') { throw 'Неверный SHA или профиль поставки' }
foreach ($entry in $manifest.files.PSObject.Properties) {
    if ($entry.Name -match '(^/|\\|(^|/)\.\.(/|$))' -or $entry.Value -cnotmatch '^[a-f0-9]{64}$') { throw 'Неверный путь или hash' }
    $file = Get-Item -Force -LiteralPath (Join-Path $Source $entry.Name)
    if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant() -cne $entry.Value) { throw 'Пакет изменен' }
}
$actual = @(Get-ChildItem -Recurse -Force -File -LiteralPath $Source)
if ($actual.Count -ne @($manifest.files.PSObject.Properties).Count + 1) { throw 'В пакете лишние файлы' }
$SourceSha = $manifest.source_sha
$Image = 'ksu-pr46-owner:' + $SourceSha.Substring(0,12)
docker build -f (Join-Path $Source 'Dockerfile.managed-postgres') -t $Image $Source
if ($LASTEXITCODE -ne 0) { throw 'Локальная сборка не прошла' }
```

## Backup и независимый restore в согласованном окне

Только после подтверждения **существующей PG17**, актуальной приватной конфигурации backup-роли
и прекращения бизнес-записи. Env-файл: `KSU_PG_BACKUP_DSN` и прежние транспортные параметры;
для owner-PC external Amvera — `amvera-external`, точный pinned host, `sslmode=require`.
Не использовать internal plaintext с ПК; не менять runtime DSN. Нужный pinned образ PostgreSQL
уже используется проектом; pull ниже загружает бесплатный локальный image, не создаёт сервис.

```powershell
$BackupEnv = Read-Host 'Полный путь к существующему приватному backup.env вне Git'
$PrivateRoot = Join-Path $env:LOCALAPPDATA ('INSTRAKT\Release-private-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $PrivateRoot | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $PrivateRoot -AclObject $acl
$BackupDir = Join-Path $PrivateRoot 'backup'
$RestoreDir = Join-Path $PrivateRoot 'restore-test'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OwnerTools 'tools\Invoke-ManagedPostgres.ps1') -Operation preflight -Role backup -EnvFile $BackupEnv -OutputDirectory (Join-Path $PrivateRoot 'backup-preflight') -Image $Image
if ($LASTEXITCODE -ne 0) { throw 'Backup preflight отказал' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OwnerTools 'tools\Invoke-ManagedPostgres.ps1') -Operation backup -Role backup -EnvFile $BackupEnv -OutputDirectory $BackupDir -Image $Image
if ($LASTEXITCODE -ne 0) { throw 'Backup не прошел' }
$BackupFile = Join-Path $BackupDir 'backup.ksubackup'
$BackupHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BackupFile).Hash.ToLowerInvariant()
docker pull postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5
if ($LASTEXITCODE -ne 0) { throw 'Образ локального restore недоступен' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OwnerTools 'tools\Test-ManagedPostgresBackup.ps1') -Backup $BackupFile -OutputDirectory $RestoreDir -Image $Image
if ($LASTEXITCODE -ne 0) { throw 'Независимый restore не прошел' }
$proof = Get-Content -Raw -LiteralPath (Join-Path $RestoreDir 'restore.json') | ConvertFrom-Json
if ($proof.verified -ne $true -or $proof.sessions_revoked -ne $true -or $proof.package_sha256 -cne $BackupHash -or @($proof.tables.PSObject.Properties).Count -ne 16) { throw 'Недостаточное подтверждение восстановления' }
Write-Host 'PASS: независимое локальное восстановление 16 таблиц; production не заменялась'
```

`ExecutionPolicy Bypass` действует только для вызванного процесса; глобальную политику
Windows команды не меняют. Защитить диск с копией и локальными env, сохранить вторую копию.
Скрипт оставляет приватные локальные ресурсы и останавливает **только новый локальный** PG;
удалять их потом по точным именам, не через общий prune. Ошибка — STOP, не повторять с force/reset.

## Поставка после отдельного GO

**Сейчас не выполнять этот раздел.** До него владелец должен заполнить лист GO, сохранить
прежнюю конфигурацию и code package, исключить очереди/запись, подтвердить свежий restore.
В кабинете существующего приложения установить analytics **off**, не менять остальные флаги,
DSN/секреты, origin, deployment ID, тариф или реплики. Убедиться, что другая автосвязь не
перезапишет доставку. Если это ещё не существующий Git Amvera способ — сначала согласовать
его настройку, не выполнять команды по догадке.

Подготовка **локального** delivery commit в новом каталоге, без push. Git использует обычную
приватную авторизацию владельца, URL не содержит пароль/токен. Исходники приложения GitHub
не заменяются; сохраняется линейная история Git существующего сервиса Amvera.

```powershell
$AmveraGit = (Read-Host 'HTTPS Git URL из вкладки Репозиторий существующего приложения').Trim()
if ($AmveraGit -cnotmatch '^https://git\.amvera\.ru/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?$') { throw 'Неверный URL существующего Git Amvera' }
$Delivery = Join-Path $env:LOCALAPPDATA ('INSTRAKT\PR46-delivery-' + [Guid]::NewGuid().ToString('N'))
git clone --no-checkout --single-branch --branch master $AmveraGit $Delivery
if ($LASTEXITCODE -ne 0) { throw 'STOP: источник/ветка master не подтверждены' }
$BeforeDelivery = (git -C $Delivery rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить прежний delivery SHA' }
git -C $Delivery branch owner-before-pr46 $BeforeDelivery
if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить точку возврата кода' }
$RollbackZip = Join-Path (Split-Path -Parent $Delivery) ('PR46-before-' + $BeforeDelivery + '.zip')
git -C $Delivery archive --format=zip --output=$RollbackZip owner-before-pr46
if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить прежний исходный код' }
git -C $Delivery read-tree --empty
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить локальный индекс' }
Get-ChildItem -Force -LiteralPath $Source | Copy-Item -Destination $Delivery -Recurse
git -C $Delivery add --all -- .
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить пакет' }
git -C $Delivery diff --cached --stat
```

Проверить прежний код/manifest и diff **приватно**. Он должен точно соответствовать полной
замене исходников source bundle, без данных/секретов. Если текущий `master` не соответствует
работающей сборке или в старой поставке есть неподтверждённые изменения — STOP. Commit сам
не запускает Amvera; в сообщении указан исходный SHA, а не исходник произвольной ветки.

```powershell
git -C $Delivery commit -m "Deliver accepted PR46 source $SourceSha with managed PostgreSQL"
if ($LASTEXITCODE -ne 0) { throw 'Delivery commit не создан' }
$DeliveryCommit = (git -C $Delivery rev-parse HEAD).Trim()
```

**Следующая команда сразу запускает сборку/deploy в Amvera. Только отдельное разрешение
владельца на этот SHA и это окно даёт право выполнить её. Codex её не выполняет.**
Обычный push отклонится, если удалённая ветка успела измениться; не добавлять force.

```powershell
git -C $Delivery push origin HEAD:refs/heads/master
if ($LASTEXITCODE -ne 0) { throw 'Поставка не подтверждена; проверить состояние в кабинете, не применять force' }
```

После успешной сборки открыть прежний адрес: health.build = `$SourceSha`, backend PostgreSQL.
Далее короткий smoke, затем отдельное включение **`KSU_WRITEOFF_ANALYTICS_ENABLED=on`** во
вкладке переменных и штатное применение/restart приложения, по [плану](RELEASE_GO_NO_GO.md#5-окно-обновления-backup--off--smoke--on).

## Откат кода при ошибке после поставки

Сначала закрыть запись/импорт, сохранить свежую копию **текущей** PG, в кабинете analytics=off.
Не откатывать БД и не менять origin/ID. Раздел применим только к подтверждённо совместимому
старому коду и сохранённому delivery checkout. После чужих commit/других изменений — STOP.
Команды используют переменные из того же приватного сеанса, возвращают прежний code tree
обычным revert (без переписывания истории); последний push опять является deployment.

```powershell
if ((git -C $Delivery rev-parse HEAD).Trim() -cne $DeliveryCommit) { throw 'После поставки изменился checkout; автоматический revert запрещен' }
git -C $Delivery revert --no-edit $DeliveryCommit
if ($LASTEXITCODE -ne 0) { throw 'Revert не прошел; не использовать force/reset' }
git -C $Delivery diff --exit-code owner-before-pr46 HEAD -- .
if ($LASTEXITCODE -ne 0) { throw 'Дерево отката отличается от сохраненного кода' }
```

После проверки и отдельной команды владельца на возврат прежнего кода:

```powershell
git -C $Delivery push origin HEAD:refs/heads/master
if ($LASTEXITCODE -ne 0) { throw 'Откат не подтвержден; проверить кабинет, не применять force' }
```

Проверить прежний health source SHA, вход, старый акт/фото/receipt. Новые данные PR46 остаются
в той же PG и снова видны после совместимого включения; предрелизный backup поверх неё не
восстанавливать. При повреждении данных нужен отдельный план со сверкой новых записей.
