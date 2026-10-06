# Федерация и межсерверный протокол Misbot 1

Продукт: **1.0.0**. Версия протокола Misbot: **1**, независимо от Matrix и формата конфигурации.
Это протокол Misbot, а не стандарт Matrix. Схема HTTP: [OpenAPI](misbot-api.openapi.json).

Управление разделено на три операции:

* `sudo misbot federation add --server example.net` добавляет только локальную запись allowlist.
* `sudo misbot federation invite --server example.net` инициирует взаимное согласование двух экземпляров.
* `sudo misbot federation rooms invite --server example.net --room '!service:example.org'` приглашает согласованный аккаунт бота в комнату после подключения.

Matrix server_name здесь — доменное имя без схемы/пути/порта, в соответствии с действующей моделью установщика. IP и произвольные API URL не принимаются. Другие формы Matrix server_name этим продуктовым маршрутом не поддерживаются.
Проверка `sudo misbot federation check --server example.net` показывает синтаксис, Matrix discovery и HTTPS версии endpoint, отдельно Misbot discovery/TLS и присутствие в локальной политике. `federation_exchange: not_verified` означает, что комнаты/пользователи по федерации не проверялись. DNS/timeout не означают отсутствие сервера.

## Локальная политика и recovery

`sudo misbot federation servers` показывает FEDERATION_SERVERS из `settings/policy.env` (либо прежнего settings.env).
`federation add/remove --server DOMAIN` сохраняет остальные записи, делает согласованную копию и проверяет восстановление, сохраняет прежние пользовательские/generated настройки в приватном `stages/federation/ID`, обновляет только политику Synapse и каталог комнат Web, перезапускает Synapse/Web и проверяет ответ Synapse.
Повтор add/remove безопасен. Удаление последней внешней записи оставляет собственный домен: федерация не открывается всем.
Matrix identity, БД, медиа, OAuth и ключи не меняются.
При прерванной публикации используйте `sudo misbot federation recover`; для возврата прежнего локального набора — `sudo misbot federation rollback`.
Сначала дождитесь окончания busy операции; не удаляйте lock.
Ручные изменения проходят `misbot config validate` и штатный `misbot config apply`, как описано в [конфигурации](configuration.md).

## Заявки и подтверждение

1. На A: `sudo misbot federation invite --server b.example.org` (местное одобрение A).
2. На B: `sudo misbot federation requests`; затем `sudo misbot federation requests approve --request MB-0123456789abcdef0123456789abcdef`.
3. На каждой стороне: `sudo misbot federation requests retry --request MB-0123456789abcdef0123456789abcdef` для продолжения и запроса удалённого статуса.
4. `sudo misbot federation peers` показывает согласованные identities. `requests status --request ID` читает локальный checkpoint.

ID имеет вид `MB-` и 32 строчных hex символа, сохраняется на обеих сторонах. Default срок — семь суток, UTC Unix timestamp в статусе. Возможности протокола v1: только взаимный allowlist и согласованная bot identity.
Состояния: `pending`, `accepted`, `applying`, `partially_applied`, `connected`, `rejected`, `expired`, `cancelled`.
`connected` подтверждает применение политики обеими сторонами через подписанный статус. Это **не** приёмка федеративных комнат и пользователей.
Неатомарность двух серверов отражается в local_applied/remote_applied. Потеря ответа сохраняет заявку; retry не создаёт вторую заявку того же исходящего подключения.
Одновременные встречные заявки имеют разные ID и независимые одобрения, применяются идемпотентно.
Отказ: `requests reject --request ID`; отмена: `requests cancel --request ID`. Отмена согласования сохраняет ранее применённые и независимо добавленные записи. Их удаляет только отдельная местная `federation remove`.
Бот создаёт отдельную зашифрованную служебную комнату и приглашает первоначального администратора. Примите её в Matrix-клиенте. Других текущих местных администраторов уведомляет в их приватных комнатах с ботом; пригласите его и подтвердите комнату через CLI, если такой комнаты ещё нет.

## Административный Matrix-бот

Аккаунт `@federation-bot:example.org` обычный, отдельный от `@referral-bot:example.org`. Его собственная сессия не имеет admin scope. Защищённая host-служба проверяет актуальные серверные права через MAS — источник прав при действующей native OAuth интеграции. Проверяется местный server_name, активность, locked/deactivated и серверное admin право, включая повтор перед отложенным применением. Power level комнаты не даёт разрешения. Недоступность проверки означает отказ.
Управляющий сокет допускает только выделенный UID 10003 и фиксированные операции. У бота нет Docker socket, host root volume или произвольного shell. У HTTP API нет административного dispatcher.

Команды без слешей:

```text
помощь
серверы
проверить сервер example.net
добавить сервер example.net
удалить сервер example.net
пригласить сервер example.net
заявки
операции
подтвердить MB-0123456789abcdef0123456789abcdef
отклонить MB-0123456789abcdef0123456789abcdef
отменить MB-0123456789abcdef0123456789abcdef
повторить MB-0123456789abcdef0123456789abcdef
статус MB-0123456789abcdef0123456789abcdef
приглашения
принять комнату !service:example.org
отклонить комнату !service:example.org
покинуть комнату !service:example.org
пригласить бота example.net !service:example.org
автоприём включить
автоприём выключить
восстановить
откатить
```

Собственные сообщения, edits и повторные события не выполняют повторное изменение. После сбоя используйте явный retry/recovery; replay исходного сообщения не является retry.
Шифрование использует существующий matrix-bot-sdk/Rust crypto SQLite и сохранённое устройство. Сессия/crypto store не заменяются при запуске.
Приглашения адресованы только самому боту. Список/подтверждение/отказ/выход: `federation rooms invitations/accept/reject/leave` (для последних трёх нужен `--room`). Отозванное приглашение возвращает `no_current_invitation`.
`sudo misbot federation rooms auto-accept --enabled true` включает `FEDERATION_AUTO_ACCEPT=true` в общей policy.env; default **false**. Автоматика требует connected request, одобренного peer, точного согласованного bot MXID и присутствия server_name в локальном allowlist. Совпадения локального имени недостаточно.
Отказ Matrix при вступлении не обходит политику: проверьте `federation servers`, согласуйте заявку и повторите rooms accept.

## HTTPS discovery и доверие

`GET https://example.org/.well-known/misbot` публично возвращает protocol, instance_id, server_name, bot_mxid, api_path, algorithm, public_key и key_id. Не требует Matrix-соединения/allowlist.
`POST https://example.org/_misbot/v1/exchange` принимает подписанные действия `invite`, `status`, `decision`, `rejected`, `cancelled`. Подтверждение местным оператором выполняется только через CLI/UI/бота; удалённый `decision` сообщает одобрение удалённой стороны.
Межсерверная отмена не удаляет локальную политику и не отменяет независимые заявки.

Каждая установка генерирует отдельный RSA-3072 private key. RSA/SHA256 подпись PKCS#1 v1.5 покрывает canonical JSON payload (sort_keys, ASCII escapes, без пробелов), включая method/path, sender/recipient и их instance IDs, timestamp ±300 s, random nonce и body. Ответ также подписан и связан с request nonce через reply_to. Общего встроенного секрета нет.
Первое discovery аутентифицируется обычным HTTPS для ровно заявленного server_name; ключ/instance ID закрепляются при одобренном подключении и внутри каждой заявки. Смена закреплённой identity требует отдельного административного решения; автоматической подмены нет.
Транспорт разрешает только фиксированные origin/path, порт 443, публичные IP. Все результаты DNS проверяются; соединение использует один из проверенных адресов без повторного DNS, TLS сохраняет исходное имя/SNI. Redirects не выполняются. Ключи, URLs или response bodies ошибок не записываются в operational logs.
Лимит тела/ответа 32 KiB, 30 запросов/мин на локальный listener; 10 s timeout сетевого обмена, nonce хранится 10 min. Лимит сохранённых заявок 1000, событий 100000: при достижении требуется операторская оценка архива, автоматическая очистка не реализована.
Нужны OpenSSL (штатная зависимость установщика), синхронизированное время и рабочий HTTPS сертификат. При signature/expired_request проверьте время/NTP, protocol и публичный ключ peer, затем requests retry. При peer_identity_changed не обходите pin: выясните восстановление/замену удалённой установки.
Матрица/OAuth HTTP маршруты сохраняются; API расположен на основном домене до общего redirect, новый поддомен не требуется.

## Постоянное состояние и восстановление

* `data/federation/identity.pem`, `state.json` — private key, instance identity, peers, заявки, nonce/event checkpoints (root 0700/0600).
* `config/federation-bot.json`, `secrets/federation-session.json`, `data/referral-control/federation-session.json` — приватная обычная bot session.
* `data/federation-bot/` — отдельное устройство, sync state, crypto SQLite, cross signing (UID 10003, 0700).
* `stages/federation/ID`, `federation-transition.json` — приватное локальное recovery.
* `run/federation/` — временные Unix sockets, в backup не входят.

Обычная согласованная копия включает это состояние; бот останавливается вместе с писателями, API/backend сериализуются общей блокировкой. Проверка восстановления проверяет identity/session/crypto files. Staged restore не запускает ни бот, ни API до `backup activate --confirm source-stopped`; freeze останавливает host controller. Systemd unit привязан к домену и конкретному каталогу, чужое назначение не перезаписывается. Перед активацией на другой машине необходимо остановить источник: распределённого lease между независимыми VPS нет, это существующий обязательный activation gate.
При переносе в другой каталог на одной машине выполните `sudo misbot --directory /путь/источника down`: команда освобождает свой Compose slot и удаляет только свой остановленный controller unit, сохраняя данные и identity. Обычный `stop` сохраняет привязку unit к источнику.
В SSH UI раздел «федерация/бот/заявки» вызывает те же команды. Help и version работают без секретов/Docker.

## Вымышленный пример обмена

```json
{"payload":{"protocol":1,"sender":"a.example.org","instance_id":"0123456789abcdef0123456789abcdef","recipient":"b.example.org","recipient_instance":"abcdef0123456789abcdef0123456789","timestamp":1893456000,"nonce":"1234567890abcdef1234567890abcdef","method":"POST","path":"/_misbot/v1/exchange","body":{"action":"invite","request_id":"MB-0123456789abcdef0123456789abcdef","expires_at":1894060800,"approved":true,"applied":false}},"signature":"BASE64_RSA_SIGNATURE"}
```

Подпись примера — placeholder, а не действующий запрос. Получатель отвечает такой же подписанной оболочкой: body содержит request_id, reply_to (nonce запроса), state, approved и applied.
Реальный обмен выполняет `misbot federation invite`, оператор не переносит private key между серверами.

Используемые механизмы: [Synapse server admin API](https://element-hq.github.io/synapse/latest/usage/administration/admin_api/user_admin_api.html),
[SDK crypto store](https://github.com/turt2live/matrix-bot-sdk/blob/main/examples/encryption_bot.ts).

## Изменение через бота и остановка для копии

Изменение через бота сначала получает сохранённый operation ID `FO-` плюс 32 hex символа и статус queued. Узкая служба независимо выполняет фиксированную операцию после повторной проверки прав. Копия может остановить и перезапустить сам бот; команда от этого не теряется. Результат сохраняется и приходит уведомлением после его запуска.
`операции` в боте или `sudo misbot federation operations` показывает queued/running/complete/failed/interrupted. Перезапуск controller не запускает повторно running операцию: сначала её checkpoint и явный recovery/retry. Duplicate Matrix event сохраняет тот же operation ID, не создавая вторую работу. Операции CLI выполняются синхронно.
