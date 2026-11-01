# Развёртывание ИНСТРАКТ · КСУ Контроль

Активное направление задачи №4 — **Amvera**, бюджет до 500 ₽ за 30 дней, только синтетические данные. Oracle больше не используется; Oracle-специфичный PR не требуется объединять.

Полная конфигурация, смета с ограничениями, авторизация, автоматический выпуск, резервирование, результаты проверок и оставшиеся условия приёмки: [AMVERA.md](AMVERA.md).

- `amvera.yml`, `Dockerfile` — образ, порт 8000 и постоянный `/data`;
- `deploy/amvera/runtime.py` — одна реплика, отказ эфемерного хранения, UID 10001, периодические копии;
- `server/storage.py` — защита от превышения объёма данных; SQLite и фото на одном постоянном томе;
- `server/maintenance.py` — проверенные атомарные snapshots/restore без перезаписи существующего файла;
- `deploy/amvera/container_check.py` — реальный тест 0,5 CPU/1 GiB, фото/отчёты, новый контейнер с прежним volume и восстановление;
- workflows `ksu-tests`, `ksu-amvera-release`, `ksu-amvera-backup` — проверки и выключенные до авторизации/одобрения доставки и копии.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python deploy/get_caddy.py --output /tmp/ksu-caddy
CADDY_BIN=/tmp/ksu-caddy .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python smoke_test.py
.venv/bin/python v11_flow_test.py
docker build -t ksu-amvera:check .
.venv/bin/python -m deploy.amvera.container_check --image ksu-amvera:check --caddy /tmp/ksu-caddy --output /tmp/ksu-amvera-metrics.json
```

В управляемой среде сборка использует настроенный прокси и build-only CA secret (`--secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt`). Не отключать TLS/checksum/signature verification. Обычная сборка Amvera/GitHub использует системное доверие и не требует сессионного CA.

Публичный Amvera-пилот пока не развёрнут; платные ресурсы не создавались. Нужны авторизация существующего аккаунта, применение разрешений сети и отдельное подтверждение конкретного тарифа/запуска после проверки цены агентом. Команды и технические настройки выполняет агент, не владелец.
