# Команда Misbot 1.1.1

Для индивидуально подключённой существующей установки действует ограниченный
[режим Compose](existing-compose.md): `misbot help` показывает доступные команды;
обычный init, генерация настроек и автоматическое обновление компонентов запрещены.
Этот backend поддерживает S3 медиа и потоковые шифрованные внешние копии;
`storage status` показывает их состояние. После первой проверенной внешней копии
`backup create` использует сохранённый S3-профиль. Подробности в linked guide выше.

После успешного `config init` установщик создаёт `/usr/local/bin/misbot` и
публичный runtime справки в `/usr/local/lib/misbot`. Команда выбирает записанный
каталог установки независимо от текущего каталога shell. Другую установку
выбирайте явно: `sudo misbot --directory '/opt/Matrix Test' status`.
Чужая команда, изменённые файлы wrapper и другой владелец default не перезаписываются.
`help`, `version` и справка операций не требуют root, Docker или чтения секретов.
Остальные операции требуют root. Используйте `sudo`; команда сама пароль не
запрашивает. В скрипте используйте `sudo -n`, иначе сначала войдите как root.
Отказ прав возвращает 77, неверный CLI/каталог — 2; backend сохраняет свой exit code.
При пропущенном обязательном аргументе CLI указывает его имя и команду справки:
`misbot federation check` требует `--server`; пример проверки:
`sudo misbot federation check --server example.org`. Значения ошибочного ввода
в диагностике CLI не выводятся.

До регистрации команды выполните **из распакованного комплекта**:

```bash
python3 misbot.py help
python3 misbot.py version
sudo python3 misbot.py config init --domain example.org --directory /opt/matrix-stack
```

Затем выполните `sudo misbot ui`. В TTY без аргументов открывается мастер/меню; без TTY — краткая справка. Ручной путь: [конфигурация](configuration.md).

```bash
misbot help
misbot storage help
misbot storage s3 check --help
misbot version
sudo misbot status
sudo misbot check
sudo misbot start
sudo misbot stop
```

Группы: `config`, `users`, `storage`, `backup`, `update`, `credentials`, `federation`, `homepage`.
`federation help` описывает локальную политику, заявки и комнаты бота;
`homepage help` описывает лендинг, два режима переадресации и восстановление. Доступные операции и
обязательные аргументы показаны в help каждой группы и в `COMMAND --help`.
Для команды восстановления следуйте выводу `status`, учитывая pending и занятые
блокировки. Не удаляйте checkpoints, не выполняйте повторный init на рабочей установке.

Обновление использует доверенный распакованный комплект:

```bash
sudo misbot update apply --source /root/matrix-installer
sudo misbot update recover --source /root/matrix-installer
sudo misbot config command install
sudo misbot config command remove
sudo misbot remove
```

Успешный start, update и update-recover обновляют wrapper. `config command install`
повторно регистрирует существующую установку; для старой установки, где команды
ещё нет: `sudo python3 /opt/matrix-stack/command_wrapper.py install --directory /opt/matrix-stack`.
`config command remove` удаляет только wrapper. `remove` открывает интерактивный
мастер необратимого удаления выбранных данных и внешних ресурсов. Утилита остаётся;
нет флага `--yes`, требуется итоговое подтверждение доменом. Работает после init.
Подробно: [удаление](README.md#11-панель-приветствия-и-полное-удаление).

На новом сервере restore/fetch выполняйте через `sudo python3 misbot.py` из
распакованного комплекта. До регистрации команды так же выполняйте verify/status/
activate, явно задавая `--directory`. Активация копии этого комплекта регистрирует
wrapper после успешного запуска. В копии старого технического комплекта команды
может ещё не быть: продолжайте через bootstrap до обновления runtime, сохраняя
данные и identity; наличие misbot не подразумевается самим завершением restore.

## Проверка S3 без подключения

До настройки установки используйте из распакованного комплекта:

```bash
sudo python3 misbot.py storage s3 check --config /root/misbot-s3.env --purpose media
```

После init та же операция доступна из любого каталога:

```bash
sudo misbot storage s3 check --config /root/misbot-s3.env --purpose media
sudo misbot storage s3 check --config /root/misbot-backup-s3.env --purpose backup
sudo misbot storage s3 check --config /root/misbot-s3.env --purpose media --read-only
sudo misbot storage s3 check --config /root/misbot-s3.env --purpose media --json
```

Файл — UTF-8, root-owned, mode 0600, обычный файл без symlink/hardlink.
Каталог держите 0700. Файл **не выполняется**; значения буквальные, без shell expansion.
Допустимы только следующие шесть полей, каждое один раз:

```dotenv
S3_BUCKET=fictional-private-bucket
S3_REGION=us-east-1
S3_ENDPOINT_URL=https://s3.example.org
S3_ACCESS_KEY_ID=FICTIONAL_ACCESS_KEY
S3_SECRET_ACCESS_KEY=FICTIONAL_SECRET_KEY
S3_PREFIX=matrix/example/media/
```

Обязательны bucket, region, пара credentials и отдельный prefix с `/` в конце.
Prefix: латинские буквы, цифры, `.`, `_`, `-`, `/`, без пустых частей, `.` и `..`.
Endpoint — HTTPS origin без credentials/query/пути; пустой endpoint выбирает AWS.
Region получите у провайдера: он участвует в подписи SigV4 и не вычисляется из
имени endpoint. Session credentials не поддерживаются текущим форматом.
Если SDK отсутствует, установите `sudo apt-get install python3-boto3` и повторите.

Проверка показывает параметры файла, DNS, TCP/timeout, TLS, подписанный HEAD,
доступность bucket, запись, чтение с SHA256 и очистку. Для backup дополнительно
проверяет запрет анонимного чтения своего probe. Ключи, подписи, тела ошибок и
содержимое объектов не печатаются. HEAD 400/403/404 может означать отсутствие
bucket **или** доступа. 403 сам по себе не доказывает ошибку credentials.
При SignatureDoesNotMatch проверьте ключ приватно, region, endpoint и proxy;
при RequestTimeTooSkewed — `timedatectl status`; при TLS — hostname/CA.
При DNS/timeout/временном отказе сохраните файл и повторите ту же команду позже.
Не отключайте TLS, не меняйте credentials автоматически и не запускайте init.

Probe использует случайные вымышленные данные и уникальный ключ внутри
`S3_PREFIX + .misbot-probes/v1/`. Условная запись `If-None-Match: *` защищает
существующие объекты; неподдерживающий её провайдер не проверяется небезопасной
перезаписью. Диагностика не перечисляет пользовательские объекты.
Нужны права HEAD bucket, PutObject/GetObject/DeleteObject на собственный namespace,
а для версионного bucket — GetObjectVersion/DeleteObjectVersion.
Полная внешняя копия дополнительно требует перечисления своего backup/media
prefix и проверки восстановления БД; диагностика этих возможностей не подтверждает.
Режим `--read-only` подтверждает только HEAD и транспорт, а GET/PUT/DELETE остаются
`not_checked`. Успешный probe не доказывает полноту переноса или восстановления.

До PUT сохраняется приватный catalog в `/root/.local/state/misbot/s3-probes`
(другой приватный каталог задаётся `--catalog-directory PATH`). Он содержит
назначение, уникальный ключ, digest и VersionId, без credentials.
Если удаление не удалось, результат отдельно показывает успешность GET/PUT и
**точную команду** со своим catalog. Например:

```bash
sudo misbot storage s3 cleanup --config /root/misbot-s3.env --catalog /root/.local/state/misbot/s3-probes/PROBE.json
```

Cleanup удаляет только объект, доказав соответствие nonce/digest и назначению.
При известном VersionId удаляется только эта версия; неизвестные старые версии
не перечисляются. Повтор cleanup безопасен. Не подменяйте catalog и не используйте
его для удаления других объектов. При прерывании сохраните catalog и повторите
cleanup после исправления доступа. До init замените `misbot` на `python3 misbot.py`
в показанной команде, выполняя её из распакованного комплекта.

До первого подключения можно продолжить с локальными медиа:

```bash
sudo misbot storage local
sudo misbot start
sudo misbot storage s3 connect --config /root/misbot-s3.env
```

Последняя операция переносит имеющиеся локальные медиа с backup/recovery и
сохраняет оригиналы. На действующей S3-установке не переключайтесь на пустое
локальное хранилище; смена ключей — отдельная `credentials s3 rotate --config PATH`.
После interrupted media transfer: `sudo misbot storage recover`, затем повтор
`storage s3 connect` с прежним private config. Подробнее: [хранилища](storage.md),
[копии](backup.md), [credentials](credentials.md).

## Структурированный результат

`storage s3 check/cleanup --json` печатает один JSON-объект UTF-8, schema=1.
Поля: operation, purpose, passed, exit_code, stages (stage/status/code/explanation/
next_actions), capabilities (read/write/cleanup), conclusion, next_actions,
cleanup_pending. Возможны статусы running, passed, failed, not_checked.
Код завершения 0 — выполнены заявленные проверки, 1 — отказ/неподтверждённая
очистка, 130 — прерывание. Безопасный code не содержит сырой ответ провайдера.
JSON предназначен для будущего UI; UI не должен разбирать текст stdout.

Поведение HEAD и условной записи описано в официальной документации
[HeadBucket](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_bucket.html)
и [PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html).

## Соответствие технических операций

| CLI | Backend | Обязательные аргументы |
|---|---|---|
| `misbot federation servers` | `federation-servers` | нет |
| `misbot federation check` | `federation-check` | `--server` |
| `misbot federation add` | `federation-add` | `--server` |
| `misbot federation remove` | `federation-remove` | `--server` |
| `misbot federation invite` | `federation-invite` | `--server` |
| `misbot federation peers` | `federation-peers` | нет |
| `misbot federation requests` | `federation-requests` | нет |
| `misbot federation operations` | `federation-operations` | нет |
| `misbot federation requests status` | `federation-request-status` | `--request` |
| `misbot federation requests approve` | `federation-approve` | `--request` |
| `misbot federation requests reject` | `federation-reject` | `--request` |
| `misbot federation requests cancel` | `federation-cancel` | `--request` |
| `misbot federation requests retry` | `federation-retry` | `--request` |
| `misbot federation recover` | `federation-recover` | нет |
| `misbot federation rollback` | `federation-rollback` | нет |
| `misbot ui` | `ui` | нет |
| `misbot config init` | `init` | `--domain` |
| `misbot config generate` | `generate` | нет |
| `misbot config validate` | `configuration-validate` | нет |
| `misbot config migrate` | `configuration-migrate` | нет |
| `misbot config recover` | `configuration-recover` | нет |
| `misbot config rollback` | `configuration-rollback` | нет |
| `misbot config apply` | `configuration-apply` | нет |
| `misbot config preflight` | `preflight` | нет |
| `misbot config command install` | `wrapper-install` | нет |
| `misbot config command remove` | `wrapper-remove` | нет |
| `misbot status` | `status` | нет |
| `misbot check` | `check` | нет |
| `misbot start` | `up` | нет |
| `misbot stop` | `stop` | нет |
| `misbot remove` | `remove` | нет |
| `misbot users password set` | `user-password` | `--input` |
| `misbot users password recover` | `user-password-recover` | нет |
| `misbot users invitation ensure` | `invitation-ensure` | нет |
| `misbot users invitation status` | `invitation-status` | нет |
| `misbot users invitation show` | `invitation-show` | нет |
| `misbot users invitation revoke` | `invitation-revoke` | нет |
| `misbot users invitation new` | `invitation-new` | нет |
| `misbot credentials admin show` | `invitation-admin-show` | нет |
| `misbot storage local` | `media-local` | нет |
| `misbot storage s3 connect` | `media-s3` | `--config` |
| `misbot storage recover` | `media-recover` | нет |
| `misbot storage database connect` | `database-external` | `--config` |
| `misbot storage database recover` | `database-recover` | нет |
| `misbot storage database abort` | `database-abort` | нет |
| `misbot backup create` | `backup` | нет |
| `misbot backup recover` | `backup-recover` | нет |
| `misbot backup verify` | `restore-check` | нет |
| `misbot backup key create` | `backup-key` | `--key` |
| `misbot backup external create` | `backup-external` | `--config`, `--key` |
| `misbot backup external cleanup` | `backup-cleanup` | `--config`, `--catalog` |
| `misbot backup fetch` | `backup-fetch` | `--config`, `--key`, `--object`, `--sha256`, `--output` |
| `misbot backup restore` | `restore` | `--archive`, `--sha256`, `--public-ip` |
| `misbot backup restore-local` | `restore-local` | `--archive`, `--sha256`, `--public-ip` |
| `misbot backup activate` | `restore-activate` | `--confirm` |
| `misbot backup move export` | `move-export` | нет |
| `misbot backup move abort` | `move-abort` | `--confirm` |
| `misbot backup schedule local` | `backup-schedule-local` | `--calendar` |
| `misbot backup schedule external` | `backup-schedule` | `--config`, `--key`, `--calendar` |
| `misbot backup schedule status` | `backup-schedule-status` | нет |
| `misbot backup schedule disable` | `backup-schedule-disable` | нет |
| `misbot update apply` | `update` | нет |
| `misbot update recover` | `update-recover` | нет |
| `misbot credentials admin set` | `admin-password` | `--input` |
| `misbot credentials admin recover` | `admin-password-recover` | нет |
| `misbot credentials database rotate` | `database-password` | нет |
| `misbot credentials database recover` | `database-password-recover` | нет |
| `misbot credentials s3 rotate` | `s3-credentials` | `--config` |
| `misbot credentials s3 recover` | `s3-credentials-recover` | нет |
| `misbot credentials s3 cleanup` | `s3-credentials-cleanup` | нет |
| `misbot credentials vault connect` | `vault-connect` | `--config` |
| `misbot credentials vault sync` | `vault-sync` | нет |
| `misbot credentials vault recover` | `vault-recover` | нет |
| `misbot credentials vault auth` | `vault-auth` | `--config` |
| `misbot credentials vault disconnect` | `vault-disconnect` | нет |
| `misbot storage s3 check` | `s3-check` | `--config`, `--purpose` |
| `misbot storage s3 cleanup` | `s3-cleanup` | `--config`, `--catalog` |
| `misbot homepage show` | `homepage-show` | нет |
| `misbot homepage check` | `homepage-check` | необязательные `--mode`, `--url`, `--name`, `--description`, `--support-url` |
| `misbot homepage apply` | `homepage-apply` | те же поля; для external обязателен URL |
| `misbot homepage recover` | `homepage-recover` | нет |
| `misbot homepage rollback` | `homepage-rollback` | нет |
# Главная страница

`misbot homepage help`: `show`, `check`, `apply`, `recover`, `rollback`. По умолчанию локальный лендинг. `sudo misbot homepage apply --mode element` выбирает Element; `sudo misbot homepage apply --mode external --url https://community.example.net/` — внешний сайт; `sudo misbot homepage apply --mode landing` возвращает лендинг. Поддерживаются `--name`, `--description`, `--support-url`; неуказанные поля сохраняются. Предварительно выполните ту же команду с `check` вместо `apply`. Пересоздаётся только Caddy; результат подтверждается по HTTPS, внешний сайт не проверяется. [Настройки и recovery](homepage.md).


## Панель и GitHub Releases

`misbot admin bot show`; до создания `misbot admin bot set --tag admin-panel`.
Существующий тег неизменяем. Root может выбрать владельца через `--administrator`
и затем `misbot start` применяет конфигурацию. [Панель](admin-panel.md).
`misbot update check` сравнивает версии; `misbot update latest` скачивает готовые
release assets, проверяет checksum, manifest, версию и диск и запускает update backend.
`misbot remove --backup-config PATH` добавляет известный профиль внешних копий
к интерактивному выбору удаления. Все эти операции требуют root.
