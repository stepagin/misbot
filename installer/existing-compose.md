# Управление существующей установкой Compose

Режим `existing-compose` подключает к Misbot явно обследованный работающий стек.
Он сохраняет исходные Compose-файлы, PostgreSQL major version, Synapse, MAS,
server_name, issuer и signing key. Он не запускает обычный `config init` и не
генерирует новые credentials или настройки авторизации.

## Подготовка и регистрация

Подключение требует индивидуальной инвентаризации, защищённой резервной копии и
репетиции восстановления. Оно не является автоматическим импортом произвольного
сервера. Нужны Docker Compose, Python 3, `python3-yaml`, `python3-cryptography`,
`tar` и `ionice`. Каталог установки и runtime принадлежат root; секретные
Compose/env-файлы должны быть защищены, а права bind-mounted конфигураций должны
позволять чтение пользователю соответствующего контейнера.

В `installation.json` задаются `backend: "existing-compose"`, `domain`,
`config_format_version: 1` и объект `existing` с `format: 1`. Он содержит:

- `projects`: исходные имена проектов, относительные каталоги и соответствие
  Compose service → container name; каждый каталог содержит `docker-compose.yml`.
- `images`: принятые image IDs и registry digests. В `.misbot/pins/<project>.json`
  находятся Compose overrides с этими digest и `pull_policy: never`.
- `identity`: относительные пути `synapse_config`, `mas_config`, `signing_key`,
  SHA256 signing key и неизменяемый `issuer`.
- `critical_paths`, `raw_database_paths`, `postgres_container`, `databases`,
  полный перечень `writers`, optional `http_checks` и `external_backup_paths`.
- `backup_directory` вне установки и отдельный root-owned `backup_key` с mode 0600.

Поддерживаются bind mounts внутри принятого каталога. Чужие контейнеры, отсутствующие
данные и изменения identity/образов блокируют запуск. После проверки:

```bash
sudo python3 /opt/matrix-stack/command_wrapper.py install --directory /opt/matrix-stack
misbot help
sudo misbot config validate
sudo misbot status
sudo misbot check
sudo misbot ui
```

## Команды и резервные копии

Для Telegram доступен отдельный worker и host control service: см.
`telegram-bot/README.md` в архиве и исходники `bots/telegram/` в репозитории.
Они подключаются к действующему MAS без смены issuer. При явном включении
управление вызывает те же команды Misbot; права администратора проверяются
в MAS, запуск и копия требуют подтверждения.

Для host Telegram-бота в `existing.telegram_control` указываются `directory`
(относительный закрытый каталог конфигурации) и `worker_directory` (абсолютный
каталог worker). `backup create` сохраняет его закрытые конфигурации и online
SQLite snapshots после остановки MAS. Payload `telegram/` входит в шифрованный
архив; `backup verify` проверяет его hashes и целостность SQLite. Старые архивы
без Telegram остаются допустимыми, но не восстанавливают нового бота.

При полном восстановлении до запуска служб перенесите `telegram/config/` в
исходный закрытый каталог, `control.sqlite3` в его `data/bot-control.sqlite3`,
а `worker.sqlite3` — в исходный `worker_directory`. Конфигурация/control должны
принадлежать root, worker DB — UID/GID 10001; файлы 0600, каталоги 0700.
Установите публичный runtime из сохранённого source archive и проверенные
systemd units. Токен/proxy остаются приватными; незавершённые задания не повторяйте.

Доступны `start`, `stop`, `status`, `check`, `ui`, `config validate`, регистрация
системной команды, `backup create`, `backup verify`, `backup recover`,
`backup external create`, `storage status`, `storage s3 check/cleanup/connect`
и `storage recover`, `update apply/recover --source PATH`. CLI, SSH-меню и Telegram используют один backend и lock.
Системный Caddy и другие внешние службы не перезапускаются этими командами.
После отдельного проверенного переноса Caddy и Telegram worker можно зарегистрировать
в Compose-инвентаризации. Они тогда входят в `start`, `stop`, `status` и `check`.
Обновление Misbot само по себе сохраняет прежний режим служб.

Для Caddy `existing.proxy` содержит `format: 1`, имя зарегистрированного `container`
и три отдельных относительных каталога под `runtime/`: `config_directory`,
`data_directory` и `state_directory`. Они монтируются в `/etc/caddy` (readonly),
`/data` и `/config`. `features.caddyfile` указывает на `config_directory/Caddyfile`.
Homepage validate/reload/recovery используют этот runtime; кандидат проверяется
отдельным контейнером без сети и с копией TLS в RAM. Внешние readonly mounts
допускаются только для принятого `features.site_directory` под `/srv` и собственного
публичного сокета федерации под `/run`, с точной регистрацией в `external_runtime_mounts`.
Индивидуальная тема сайта может храниться root-only в `.misbot/features/site-template/`
как `index.html` и `site.css`; применение сайта сохраняет эту тему.
Шаблон также входит в зашифрованный `misbot-control/features/site-template/` payload
копии; при полном восстановлении верните его в `.misbot/features/site-template/`
с владельцем root, правами 0700 на каталог и 0600 на файлы. Для проверки новых
архивов с этим payload используйте Misbot 1.1.1 или новее; старые архивы допустимы.

Для Telegram в `existing.telegram_control` дополнительно задаются `worker_container`,
относительный `worker_config` и `socket_directory`. `worker_directory` после переноса
находится внутри установки. Worker получает только свой config (UID/GID 10001, 0600),
свой каталог данных (10001:10001, 0700) и readonly control socket (root:10001, 0750).
Его root filesystem readonly; Docker socket и MAS credentials в него не монтируются.
Root control остаётся отдельной host-службой. Старый host worker отключается при
переносе; обновление runtime перезапускает зарегистрированный контейнер.
Копия сохраняет действующий worker config и согласованный SQLite snapshot в прежнем
формате архива; меняющиеся SQLite/WAL исключаются из сырого дерева установки.

Перенос требует инвентаризации маршрутов, копии сертификатов/config/SQLite,
проверки candidate images и восстановления. Сохраняйте исходные units и их
включённость для отката; два Telegram polling worker одновременно не запускаются.
Незавершённый индивидуальный перенос может зарегистрировать журнал
`.misbot/runtime-migration.json`, блокирующий обычные операции до recovery плана.
Новые поля необязательны и имеют формат 1: прежние установки 1.1.0 обновляются
без преобразования layout или изменения схемы PostgreSQL/SQLite.

Для уже зарегистрированного worker source update автоматически собирает образ,
если изменились `worker.py`, `common.py`, `Dockerfile` или `requirements.txt`.
Build context содержит только эти проверенные публичные файлы; базовый образ
должен быть закреплён по digest. До переключения образ проверяется без сети,
приватных mounts и polling. Сохранённый журнал содержит прежнюю инвентаризацию,
pins и новый image ID; переключается только worker с ожиданием healthcheck.
`update recover --source PATH` возвращает прежний образ с текущей SQLite, сохраняя
новые отметки доставки. Source комплект без поддержки зарегистрированных runtime
отклоняется до создания копии и изменения файлов. Старые архивы без Telegram
и прежние source-update журналы остаются допустимыми.
Source update проверяет полный public manifest, использует последнюю проверенную
внешнюю копию при совпадении identity/images или создаёт новую, сохраняет
checkpoint, обновляет исходники, системную команду и стандартный лендинг с
сохранением настроек сайта. Индивидуальный `site-template` сохраняется;
при откате возвращаются прежние HTML и CSS стандартного лендинга. `installation.json`,
Compose/pins, credentials и работающие контейнеры сохраняются. При ошибке
выполните `update recover --source PATH` из доверенного распакованного комплекта.
Дополнительные компоненты включаются через `features enable --config PLAN.json`:
Element Admin, реферальный и административный Matrix-боты, лендинг и root-служба
федерации. План содержит проверенные публичные image IDs/manifest, отдельные
домены и пути; действующие PostgreSQL, MAS и Matrix identity сохраняются.
Перед включением нужна проверенная внешняя копия. При недостатке RAM создаётся
собственный swap 1 GiB; существующие bot sessions/crypto не заменяются.
После прерывания `features recover` возвращает публичные маршруты; повторение
с прежним планом продолжает включение. CLI/SSH вызывают общий backend федерации
и `homepage`; внешний Caddy перезагружается только для этих маршрутов.
Optional `telegram_runtime` плана задаёт `directory` в `/usr/local/lib`,
`control_unit` и `worker_unit`. Misbot проверяет владельца файлов/units,
обновляет публичный runtime с checkpoint и подключает общий backend федерации
и статус компонентов в Telegram. Credentials сохраняются root-only.
`socket_directory` этого объекта допускает явно зарегистрированный readonly
mount управляющего сокета из `/run` только реферальному боту. Каталог должен
принадлежать root:GID 10001 с mode 0750. Misbot сохраняет runtime directory
при перезапуске управляющей службы; доступ к другим внешним путям не открывается.

`backup schedule external --calendar 'daily'` включает собственный systemd timer
после проверенной копии; `backup schedule status/disable` показывает или отключает
его. Расписание и удаление старых копий не включаются автоматически. Для копии
нужна краткая остановка писателей, учитывайте её при выборе календаря.
Остальные команды обычного установщика отклоняются; обновление компонентов
требует отдельного плана совместимости и новых принятых pins.

`backup create` временно останавливает только ранее работающие `writers`, делает
логические дампы всех заданных БД и ролей, сохраняет конфигурации/медиа и шифрует
архив существующим форматом AES-GCM с ограниченной памятью. Данные одноразовых
ключей Synapse исключаются. Отведено 12 минут на создание и до 3 минут на запуск
и readiness, затем писатели
возобновляются; checkpoint сохраняется, если возобновление не подтверждено.
После прерывания используйте `backup recover`, не удаляя журнал.

`backup verify` проверяет SHA256, все authenticated frames и final frame, затем
восстанавливает дампы в отдельные новые временные БД и удаляет только эти БД.
Полный расшифрованный архив на диске не создаётся. Это не заменяет изолированную
репетицию восстановления и клиентскую проверку входа, медиа, E2EE и звонков.
Сохраняйте зашифрованную копию независимо от сервера, а recovery key — отдельно.

## S3 для вложений и копий

Root-only профили `.misbot/s3/media.env` и `.misbot/s3/backup.env` содержат
те же шесть буквальных S3-полей, что [обычные профили](storage.md). Используйте
раздельные непересекающиеся префиксы. Secrets не передаются worker Telegram.

```bash
sudo misbot storage s3 check --config /root/media.env --purpose media
sudo misbot backup external create --config /root/backup.env --key /root/recovery.key
sudo misbot storage s3 connect --config /root/media.env
sudo misbot storage status
sudo misbot backup create
sudo misbot backup verify
```

Внешняя копия использует существующий recovery key инвентаризации. Полный tar
(БД, роли, конфигурации, ключи, локальные вложения, snapshot Telegram, pins/S3
профили) шифруется AES-GCM и отправляется частями до 8 MiB через conditional PUT.
Это работает и с S3-провайдерами, не поддерживающими conditional multipart
completion. Небольшой `.manifest.json` публикуется последним; проверка читает
части последовательно, проверяя ownership, SHA256 каждой части и всего архива.
Полный архив на локальном диске не создаётся; место нужно для логических дампов
и резерва 1 ГБ. Писатели возобновляются до проверки полного GET, authenticated
frames, hashes и восстановления временных БД. Объект публикуется условно, прежние
копии не перезаписываются. Проверяется запрет анонимного доступа.

После первой успешной внешней копии профиль сохраняется в `.misbot/s3/backup.env`:
`backup create` (включая Telegram) использует его автоматически. До успеха
прежняя копия и назначение сохраняются. Новый destination обычным редактированием
профиля не активируется. Retention и расписание автоматически не включаются.
Root-only catalog хранится рядом с дампами в `backup_directory`; сохраните его
вместе с recovery key для восстановления. Все части и публичный object manifest
нужны для полного архива. Незавершённые зашифрованные части сохраняются с catalog;
автоматической очистки и retention нет. Recovery key
в архив и в S3 не отправляется. Восстановление этого layout требует изолированной
репетиции: portable `backup restore` не импортирует adopted архив автоматически.

Перед подключением медиа подготовьте root-owned расширение
`synapse/extensions/s3/{dependencies,manifest.json}` для **точного принятого
Synapse digest**: provider 1.7.0, совместимый SDK, SHA256 всех файлов и результаты
проверки импортов. Публичный адаптер ограничивает upload concurrency одним потоком
и буфером 8 MiB для малых VPS. Расширение подключается readonly через pins
override/PYTHONPATH; принятый Docker image не меняется. Не используйте образ
обычного установщика с другой версией Synapse для обхода этой проверки.

Подключение требует проверенной полной внешней копии. Кратко пересоздаётся
только Synapse, затем прежние файлы копируются онлайн и проверяются по полному
GET/SHA256. Новые загрузки ждут записи в S3; локальные оригиналы сохраняются.
Различающиеся удалённые данные не перезаписываются. При прерывании выполните
`storage recover`: до активации возвращается прежняя конфигурация, после
активации продолжается копирование. Checkpoint и удалённые объекты сохраняются.
`storage status` показывает подключение и незавершённые операции без credentials.
