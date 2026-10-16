# Необязательный управляющий Telegram-бот

Это отдельный управляющий бот с токеном BotFather, а не Telegram OAuth для входа
в Matrix. Обычная установка работает без него. При подключении существующего
экземпляра сохраняйте token, служебный MAS-клиент, обе SQLite и Matrix server_name.
Не создавайте новый MAS client и не удаляйте данные для обхода несовпадения identity.

Перед переносом остановите прежний worker и его host controller. Один token
может опрашивать Telegram только из одного worker. Если исходный control SQLite
общий с Matrix-ботом, разделите активные Telegram authorization/account записи
и Matrix runtime до включения новой копии; сохраните исходную полную SQLite.
Штатный мастер не импортирует произвольный Telegram runtime автоматически.

## Файлы и включение

Подготовьте внутри переносимой установки:

| Путь | Содержимое | Владелец и права |
| --- | --- | --- |
| `config/telegram-worker.json` | Существующие token, username, числовой Telegram client_id, server_name и необязательный proxy | 10001:10001, 0600 |
| `data/telegram-worker/` | Полное состояние worker, включая `worker.sqlite3` | 10001:10001, каталог 0700; SQLite 0600 |
| `data/telegram-control/bot-config.json` | Единственный сервер и прежние MAS client_id/client_secret, разрешённые management_actions | root:root, 0600 |
| `data/telegram-control/bot-policy.json` | Существующая политика того же сервера | root:root, 0600 |
| `data/telegram-control/data/bot-control.sqlite3` | Полная согласованная SQLite управляющей службы | root:root, каталог и его родитель 0700; SQLite 0600 |

Пути и файлы не должны быть symlink/hardlink. Существующую SQLite переносите
через согласованный snapshot, включая ещё не завершённые операции и доставку,
а не копированием открытого файла без WAL. Поле `server_name` worker, закреплённый
`state.server_binding` в worker SQLite и единственный ключ `servers` обоих
control JSON должны совпадать с `installation.json.domain`.

Привязка worker уже должна существовать; установщик проверяет её без нового
входа в Telegram. В исходной SQLite могут быть Matrix-приглашения для истории,
но активные Telegram входы не должны одновременно обслуживаться Matrix controller.
Для федерации используйте общий backend `federation_backend: "misbot"`.
Выбранные `management_actions` и политика сохраняются.

После подготовки и проверки старого остановленного экземпляра явно добавьте
в root-only `installation.json` запись:

```json
"telegram_runtime": {"format": 1, "server_name": "example.org"}
```

Замените вымышленный домен своим неизменяемым Matrix server_name. Остальные
поля metadata сохраняйте. Затем выполните `sudo misbot config generate` и
`sudo misbot start`. Формат SQLite проверяется до сборки worker; неизвестная
схема требует отдельной миграции. Первый запуск запоминает fingerprint identity;
последующая подмена token или MAS credentials отклоняется.

При генерации `stack_root` и локальные transport URLs control приводятся к
текущему каталогу и localhost. Token, MAS client_id/client_secret, политика и
SQLite не пересоздаются. Для host controller включённый runtime публикует
Synapse на `127.0.0.1:8008`, локальный MAS на `127.0.0.1:8080`; эти порты
должны быть свободны. Для внешнего MAS его URL сохраняется, локальный порт MAS
не публикуется. Runtime требует
host-пакет `python3-requests`; обычный запуск и активация восстановления через
Misbot устанавливают его при включённом Telegram.

## Жизненный цикл и копии

Worker — ограниченная служба Compose `telegram-worker`: UID 10001, read-only
root filesystem, без Docker socket и MAS admin secret. Ему доступны только
worker config, собственные данные и read-only каталог control socket.
Control работает отдельной root-службой systemd с именем, зависящим от каталога
установки. Он запускается до worker и перезапускается после принятия исходников.
`misbot start`, `misbot stop`, `misbot check`, `install.sh down` и удаление
установки учитывают этот runtime.
При обычном `stop` host controller может оставаться доступен для чтения
результатов; worker останавливается вместе со стеком.

Сборка использует только проверенные публичные исходники и закреплённый base.
Точный image ID и source fingerprints сохраняются в `telegram-images.lock.json`.
Изменение worker source при обновлении пересобирает образ. Откат возвращает
прежние исходники, controller config и pin, сохраняя более новые live SQLite.

Полная локальная или внешняя копия включает credentials/config/policy и
проверенные standalone snapshots обеих SQLite. Worker входит в общий writer
pause/recovery: возобновляются только ранее работавшие writers. Работающий
controller снимает snapshot под своим state lock через root-only RPC; другой
Bridge для экспорта не создаётся. Открытые raw SQLite/WAL повторно не архивируются.

`backup verify`, восстановление и staged activation проверяют SQLite,
server binding и сохранённую identity. На staged/frozen target Telegram не
запускается. При смене каталога control/unit/socket/mounts создаются для нового
root; прежний абсолютный root не используется. Активируйте target только после
подтверждённой остановки источника.

После запуска проверьте в настоящем Telegram `/start`, прежний вход, меню
участника и администратора и одну разрешённую операцию с подтверждением.
Готовность `/health` не подтверждает эти сценарии.
