# PR №35: viewer и SQLite 3.40.1 в релизном образе

Исправление блокирующего HTTP 503 после входа `demo-viewer` в образе
`a150419d0bfd6afa7546b0578094f57eee62672c`. Работа только в PR №35,
ветке `fix/viewer-shared-interface`. Main, Amvera, рабочая SQLite и стенды 8018/8019 не используются.

## Причина и исправление

Прежний запрос ACL вложений перечислял `inspections.results` через `json_each`.
В SQLite 3.40.1 создание этой JSON virtual table конфликтует с read-only
`sqlite3.set_authorizer()`: `vtable constructor failed: json_each`. Поэтому запрос
`/api/state` и просмотр фото viewer получали HTTP 503. В Python среды разработки
используется более новая SQLite; успешный host-прогон не проверял эту комбинацию
в production image. Прежний container smoke проверял inspector, а viewer browser
запускал сервер в Python хоста — именно этот пробел закрывает новый тест.

SQLite-ветка `media_links_sql()` теперь перебирает индексы массива рекурсивным CTE,
используя только скалярные `json_type`, `json_array_length`, `json_extract`.
PostgreSQL-запрос, схема v4, образ/зависимости, интерфейс и SQL authorizer не меняются.
Не разрешаются CREATE/INSERT/UPDATE/DELETE/PRAGMA/ATTACH и не прогреваются virtual tables
через привилегированное соединение. Проверки actor ACL, объекта самой фотографии,
объекта родительского акта/нарушения и точной ссылки остаются в том же SELECT.

Версия SQLite в закреплённом образе остаётся **3.40.1**. Отдельный прямой `json_each`
под viewer authorizer может по-прежнему показывать дефект библиотеки. Критерий
исправления — работа `media_links_sql()` и реальных viewer API/UI без virtual table,
с теми же запретами SQL. Обновление SQLite или снятие authorizer не требуется.

## Автоматическое доказательство на фактическом образе

`tests/viewer_release_container.py` обращается по HTTPS к настоящему release runtime.
Production image не получает tests/harness, исходники не подменяются bind-mount.
В `docker exec` используется Python **самого образа** для версии SQLite, запроса
ACL и проверки отказа DDL/DML/PRAGMA/ATTACH. Нет подмены `sqlite3` или фиктивного номера версии.
Каждый запрос диагностики использует свежее actor-соединение, без прогрева statement cache.
Все БД, фото, аккаунты, сертификаты, контейнеры и ephemeral loopback-порты — синтетические.

Artifact job выполняет по порядку:

1. Отдельный checkout точного `a150419`, сборку его `Dockerfile.offline-release` и
   `--expect-broken`: реальная SQLite 3.40.1, точная ошибка запроса и HTTP 503 viewer.
2. Сборку нового кандидата и тот же тест: `/api/state` 200, объект/акт/нарушения/фото
   и JSON/CSV/XLSX/PDF доступны в пределах ACL. Фото только из последнего результата
   акта тоже доступно; чужое фото и orphan на разрешённом объекте — 404.
3. Все прямые бизнес-POST/PUT/PATCH/DELETE и SQL-записи viewer запрещены. Отзыв ACL
   закрывает прежний URL фото и новую выгрузку. Manager/inspector сохраняют чтение/запись.
4. Browser 1280/390 работает именно с контейнерным сервером: вход, объект, акт с фото,
   карточка нарушения с двумя фото и отчёты/скачивания, без 5xx. Для браузера доверен
   только SPKI нового синтетического сертификата; HTTP-клиент проверяет CA и hostname.
5. Сверку отпечатков бизнес-таблиц: чтение, browser и запрещённые попытки их не меняют.

Итоги `viewer-runtime-reproduction.json` и `viewer-runtime-candidate.json` публикуются
как artifact `viewer-release-sqlite-runtime`: версия SQLite/build SHA, статусы и флаги.
DB, credentials, ключи, фото и private-логи в этот artifact не входят.
Прежние тесты обеих ролей/офлайн, PostgreSQL, Windows, images и backup сохранены.

Команды повторения в разработческой CI-среде после сборки соответствующих images:

```sh
python -m tests.viewer_release_container --image ksu-viewer:rejected-a150419 --expect-broken
python -m tests.viewer_release_container --image ksu-offline:rc --browser
```

Дополнительная SQL-регрессия `test_sqlite_attachment_array_edges_keep_parent_and_media_acl`
проверяет пустые/нетипичные элементы, последний элемент из 200, точное совпадение URL,
чужой объект и отсутствие ACL на настоящем read-only actor-соединении.

## Повторная ручная приёмка Windows: новый синтетический стенд 8020

Не пересобирать/переключать/останавливать профили 8018/8019. Не заменять их images, scripts,
state.json, SQLite или volumes. Использовать новую папку исходников и уникальные теги image:

```powershell
git clone --branch fix/viewer-shared-interface https://github.com/transformation161-droid/warehouse.git warehouse-pr35-sqlite-fix
Set-Location warehouse-pr35-sqlite-fix
$sha=git rev-parse HEAD
$image="ksu-viewer-sqlite-fix:$sha"
$seedImage="ksu-viewer-sqlite-fix-seed:$sha"
docker build -f Dockerfile.offline-release --build-arg "KSU_BUILD_SHA=$sha" -t $image .
docker build -f Dockerfile.offline-acceptance --build-arg "RELEASE_IMAGE=$image" -t $seedImage .
.\tools\Start-Release-Acceptance.ps1 -Port 8020 -Image $image -SeedImage $seedImage -CertificatePath '<ваш cert.pem>' -KeyPath '<ваш key.pem>'
```

`Start-Release-Acceptance` создаёт новый UUID/контейнер/volume и четыре новые случайные
ДЕМО-учётные записи; Source-параметра или подключения старой БД нет. Сертификат должен
быть доверенным и иметь SAN 127.0.0.1/localhost; проверку TLS не отключать. Для Android
нужен разрешённый частный `-HostAddress` с SAN этого адреса по прежней Android-инструкции.
Не использовать 8018/8019 или origin/volume старого профиля.

Из напечатанной **новой** приватной папки взять пароли локально, не отправлять accounts.json
в чат/GitHub. В отдельном профиле браузера открыть `https://127.0.0.1:8020/`:

1. Войти `demo-viewer`: нет сообщения о недоступной БД, `/api/state` отвечает 200.
2. Открыть s1, план и реестр нарушений. Фото/актов в начальном seed может не быть:
   в другом профиле `demo-manager` назначает выезд `demo-inspector`; inspector выполняет
   его с нарушением и фото. Затем viewer открывает акт, нарушение, фото и отчёты.
3. Проверить CSV/XLSX/PDF, сохранение навигации и отсутствие команд записи/импорта/offline.
   `demo-unassigned` видит пустые данные, s2 и его фото viewer недоступны.
4. У manager/inspector повторить обычный выезд и изменение нарушения; просмотр общей базы
   и прежний офлайн-сценарий не должны измениться.

Для версии SQLite только нового контейнера, где `$profile` — новый путь launcher:

```powershell
$state=Get-Content -Raw -LiteralPath (Join-Path $profile 'state.json') | ConvertFrom-Json
docker exec $state.name python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Ожидается 3.40.1 и работа viewer. SHA в `/api/health` должен совпадать с `$sha`.
Не очищать старые профили/очереди, не удалять старые volumes. Этот стенд не является deploy
на Amvera или разрешением на merge/production.
