# Аудит PostgreSQL-кандидата поверх PR №36

10.11.2026 владелец передал официальный ответ Amvera: `/data` не поддерживает SQLite WAL,
согласованная SQLite-копия требует остановки приложения, backup `/data` выполняется ежедневно.
Приложение сейчас остановлено, активных пользователей нет. SQLite-выпуск №36 приостановлен;
его зелёный CI не отменяет несовместимость реального хранилища. Новый Draft PR основан на
`00515a97174efca824e834cc8e0786cd158dc4ec`, сохраняет цепочку [№19–36](PR_INTEGRATION.md).
Ни рабочая Amvera, ни SQLite, ни `main` не менялись; облачная БД не создавалась.

| Область | Уже реализовано и сохранено | Доработка для managed production |
| --- | --- | --- |
| Backend | `PostgresDatabase`, параметризованные SQL, транзакции, optimistic versions, аудит, ограничения выездов | PostgreSQL-only entrypoint без SQLite fallback/DDL; runtime-реквизиты отдельно; проверка PG17/TLS/ролей перед стартом |
| Миграция | `prepare_sqlite`, `migrate_sqlite`, COPY в пустую схему, fingerprints, FK, sequence | Owner CLI связывает известную закрытую SQLite → приватную v4-копию → PG, проверяет все 16 таблиц и хеш исходника; отчёт приватный |
| RLS | Невладеющий runtime, политики объектов/авторов, RESTRICTIVE viewer, отсутствие изменения users/audit | Проверка NOINHERIT/NOBYPASSRLS/нет membership/DDL, отдельный offline schema-owner admin |
| Viewer | Общая принятая оболочка; whitelist полей; SECURITY DEFINER с фиксированным search_path; свои фото/отчёты | Реальные SQL/API проверки на PG под обычным schema-owner admin, без superuser в приложении |
| Фото | bytea original/thumbnail, ACL объекта и ссылки из допустимых записей, запрет orphan/чужих/отозванных URL | Миграция/restore сравнивают байтовые хеши; проверки original/thumbnail и PDF/XLSX на контейнерном PG |
| Offline | checklist versions, media keys, packages, operation receipts, транзакционные повторы/конфликты | Старый receipt/пакет используются после миграции; повтор completion после restore не создаёт дубль |
| Backup | Consistent exported snapshot + native pg_dump, schema/16 fingerprints, SHA, sessions excluded, trusted pg_restore | NOBYPASSRLS backup с явными SELECT policies + `--enable-row-security`; offline credentials; независимый restore |
| Права restore | Восстановление `--no-owner --no-acl`, явные grants | Закрытие PUBLIC EXECUTE на функциях и PUBLIC grants после restore; default privileges; проверка запрета служебных функций runtime |
| Диск | Caps PG database 256 МиБ, package 512 МиБ, streaming/reserve/timeouts | Никаких SQLite/backup/locks на `/data`; локальный временный scratch, owner Linux volumes для миграции/restore |
| Доставка | Pinned Python/PG17 clients, non-root, source-only bundle, SHA | Профиль `postgresql`, Dockerfile.managed-postgres; старый SQLite bundle не применять |

Границы доверия: сотрудники не получают PostgreSQL-реквизиты. Web runtime доверен задавать
`ksu.actor` после проверки сессии; RLS дополняет API ACL, но не защищает от полного захвата
runtime-учётной записи/исполняемого кода. Admin владеет схемой `ksu`, поэтому хранится только
у ответственного за миграцию. Backup видит полные данные/хеши паролей, но не меняет их.
Все три роли LOGIN, NOSUPERUSER/NOBYPASSRLS/NOINHERIT/NOCREATEDB/NOCREATEROLE/NOREPLICATION;
служебные роли не входят в другие роли. Для bootstrap нужен отдельный владелец новой БД с
правом CREATE ROLE; его реквизитов в приложении нет. Прежний BYPASSRLS backup поддерживается
историческими инструментами, но managed production preflight его отвергает.

SECURITY DEFINER-функции принадлежат offline admin, используют фиксированный search_path и
не публикуются для PUBLIC. Admin как владелец обходит обычный RLS своей схемы — это необходимо
для миграции, аудита и viewer projection. Runtime не владеет ни схемой, ни объектами. Добавлять
FORCE RLS без переработки owner-функций нельзя. Клиентское поле role не меняет SQL actor_role.

Принятые `release_online/`, `offline/`, assets, viewer projection/ACL и offline-контракты
сохранены; UI/PWA не тестируются повторно. Общие API не расширяют объём первого релиза:
прежние ограничения decision/writeoff routes и планирования inspector остаются. Native
PG-контракты тестируются без браузера, включая upstream backend прежних этапов.

Ограничения проверки: контейнерный TLS PG17 — это реальный PostgreSQL, но не созданная Amvera.
У провайдера ещё нужно подтвердить версию 17, CA/SAN/доступ с приложения и владельца, CREATE ROLE,
pg_dump/restore и доступность штатного PG backup. Документация Amvera предупреждает об ошибке
ALPN у libpq17 на некоторых внешних endpoints. Кандидат использует закреплённые libpq/clients17;
никакого `sslmode=prefer/require` или отключения проверки CA/hostname для обхода ошибки нет.
До успешного preflight **теми же образами** на одобренном endpoint перенос не начинать.

[План и расходы](POSTGRES_PRODUCTION.md) · [Короткие действия владельца](OWNER_POSTGRES_CHECKLIST.md).
