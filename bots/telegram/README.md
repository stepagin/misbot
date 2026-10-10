# Telegram: управление через Misbot

`worker.py` принимает команды в личном чате. `bridge.py` проверяет вход через
MAS device authorization и текущие права аккаунта. Worker не получает
MAS admin secret, Docker socket, пароли БД или recovery key.

## Реквизиты и адреса

Приватный `worker.json` содержит `token` из BotFather (формат `числа:строка`),
`username` без `@`, `client_id` — числовой ID Telegram-бота — и `server_name`.
OAuth client secret Telegram нужен отдельно для Telegram Login; worker его
не использует. Для proxy добавьте `proxy`, например
`socks5h://proxy.example.org:1080`. SOCKS требует Python-пакета PySocks.
Не включайте секретные файлы в Git или журналы.

В `bot-config.json` управляющей службы `servers` сопоставляет Matrix
`server_name` с независимым `mas_url`, `synapse_url` и служебными
`client_id`/`client_secret` **MAS**. Адрес MAS может отличаться от Matrix-домена.
Служебный клиент должен поддерживать device authorization и быть перечислен
в MAS `policy.data.admin_clients`; сохраняйте существующие clients и issuer.
Рядом нужны `bot-policy.json` и закрытый каталог `data/` для SQLite.

## Команды управления

Управление включается явно: `stack_root` и
`management_actions: ["status", "check", "start", "stop", "storage", "storage_connect", "backup", "backup_verify"]` в серверном
`bot-config.json`. Оно рассчитано на принятый стек `existing-compose`:
`control.py` вызывает фиксированные команды Misbot с его блокировками и recovery.
В Telegram: `/status`, `/check`, `/storage`, `/start_services`, `/stop_services`,
`/backup`, `/backup_verify`; кнопка подключения S3 использует только заранее
заполненный root-only `.misbot/s3/media.env`. Запуск, остановка, подключение S3,
создание и проверка копии требуют одноразового подтверждения и выполняются в фоне; кнопка
показывает результат. После перезапуска службы незавершённые задания отмечаются
как interrupted и автоматически не повторяются. Проверяйте их через SSH.

До включения общих компонентов задайте `federation_enabled: false`: прежний
обработчик федерации ожидает другой layout файлов. После `features enable`
настройка `federation_backend: "misbot"` вызывает общий backend и сохранённую
очередь федерации. Текущие MAS-права перепроверяются перед постановкой задания
и выполнением; одно подтверждение даёт одну операцию. Результат доступен через
административного Matrix-бота и `misbot federation operations`. Действующий
allowlist не меняется при подключении. Action `components` добавляет кнопку
статуса компонентов и ссылки на веб-панель и сайт.
Приглашения не изменяют
действующий режим регистрации. Меню управления доступно только MAS-администраторам.

## Запуск и проверка

На Linux worker можно запускать отдельным непривилегированным systemd user ID
с `LoadCredential=worker.json:/PRIVATE/worker.json`, собственным каталогом данных
и `MISBOT_CONTROL_SOCKET=/run/misbot-telegram/control.sock`. Аргументы worker:
`--config PATH --data PATH --health-address 127.0.0.1 --health-port 8088`.
Bridge запускается root с `--config PATH --socket-directory /run/misbot-telegram`.
Socket допускает worker UID/GID 10001; не выдавайте этому UID доступ к Docker.

`/health` подтверждает свежий успешный Telegram polling и доступность socket.
После запуска проверьте реальный `/start`, вход через MAS и административное меню.
Локальные тесты: `python3 -m unittest discover -s bots/telegram/tests -p 'test_*.py'`.

`images.lock.json` фиксирует ранее собранный Docker-образ, а не текущий host
runtime. После изменения исходников для Docker требуется новая сборка и новый
lock; старый image ID не подтверждает наличие новых команд.
