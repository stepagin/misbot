# Команда Misbot 1.2.0

Справочник для администратора, который управляет установкой по SSH. Для пошаговой работы откройте
`sudo misbot ui`; команды ниже подходят для точного повторения операции и автоматизации. Установка с
нуля описана в [README](README.md), повседневная диагностика — в [памятке](administration.md).

| Задача | Где искать |
| --- | --- |
| Выбрать установку, права и запуск до установки | [Вызов команды](#вызов-команды) |
| Открыть мастер или меню | [Управление через терминал](#управление-через-терминал) |
| Найти аргументы и продолжить незавершённую операцию | [Справка и команды](#справка-и-команды) |
| Обновить runtime, зарегистрировать или удалить команду | [Обновление и системная команда](#обновление-и-системная-команда) |
| Проверить S3 до подключения | [Проверка S3](#проверка-s3-без-подключения) |
| Сопоставить публичную команду и backend | [Техническая таблица](#соответствие-технических-операций) |

## Вызов команды

Каталог выбирается через `--directory`; по умолчанию — записанная установка или `/opt/matrix-stack`.
При отсутствующем runtime запускайте `sudo python3 misbot.py …` из проверенного распакованного
комплекта и явно задавайте каталог назначения.

Для индивидуально подключённой существующей установки действует ограниченный [режим Compose](existing-compose.md): `misbot help` показывает доступные команды; обычный init, генерация
настроек и автоматическое обновление компонентов запрещены. Этот backend поддерживает S3 медиа и
потоковые шифрованные внешние копии; `storage status` показывает их состояние. После первой
проверенной внешней копии `backup create` использует сохранённый S3-профиль. Ограничения и порядок
подключения описаны в его руководстве.

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

Для операций с сохранёнными профилями можно не повторять реквизиты:
`sudo misbot backup external create` использует настроенные S3 и ключ,
а команды расписания — сохранённое время. Если данных ещё нет, в интерактивном
терминале открывается форма. Так же доступны формы подключения PostgreSQL/Vault,
смены ключей S3 и паролей, выбора аккаунта или заявки. В скрипте передайте
недостающие аргументы явно: без TTY команда не ждёт ввода. Явные параметры
сохраняют приоритет, включая `storage s3 check --read-only`.

Отсутствующий ключ старой резервной копии автоматически не заменяется.
Для её расшифрования нужен прежний ключ; отдельный новый ключ можно выбрать
только для будущих копий. `backup create` на обычной установке создаёт локальный
архив; для S3 используйте `backup external create`.

До регистрации команды выполните **из распакованного комплекта**:

```bash
python3 misbot.py help
python3 misbot.py version
sudo python3 misbot.py config init --domain example.org --directory /opt/matrix-stack
```

Затем выполните `sudo misbot ui`. В TTY без аргументов открывается мастер/меню; без TTY — краткая справка. Ручной путь: [конфигурация](configuration.md).

## Управление через терминал

На новом сервере UI предлагает установку, восстановление из копии или вход в меню.
Вход в меню не создаёт сервер. После удаления недоступные действия остаются
видимыми с объяснением причины.
Сохранённая настройка продолжается с прежнего шага. Перед запуском нужно
проверить итог и явно ввести `применить`; пустой ввод установку не запускает.
Индикатор показывает завершённые шаги мастера, а не оставшееся время установки.

После ввода настроек Telegram мастер проверяет три HTTPS-запроса к службе
входа через выбранный SOCKS5 или напрямую. Ошибка соединения позволяет
исправить прокси, повторить проверку или отключить Telegram. Проверка
подтверждает доступность во время запросов; Client ID, Client Secret и адрес
возврата окончательно проверяются входом через Telegram после запуска сервера.
Эти реквизиты OIDC отличаются от токена Bot API.

На действующем сервере откройте нужный раздел:

- **Состояние и работа сервера** — проверка, запуск и остановка.
- **Пользователи и приглашения** — код для регистрации и пароли пользователей.
- **Резервные копии** — создать, проверить, настроить расписание или перенести сервер.
- **Медиа и базы данных** — хранилище, S3, PostgreSQL и ключи доступа.
- **Сайт сервера** — тексты страницы и способ открытия чата.
- **Связь с другими серверами** — подключение и заявки.
- **Настройки сервера**, **Обновление Misbot** и **Администраторы и доступ**.
- **Продолжить прерванную операцию** — сначала проверить состояние, затем выбрать
  указанное сервером продолжение в соответствующем подразделе.

Выбирайте номер действия. `0` и `назад` возвращают к предыдущему меню; `сохранить и выйти` завершает
UI. В формах Enter сохраняет текущее значение, в подтверждениях Enter отменяет действие. Перед
изменением показываются выбранная установка, параметры и возможный перерыв в работе. Приглашение и
пароль выводятся только после выбора соответствующего действия и подтверждения. В разделе
**Пользователи и приглашения** действие **Пригласить пользователя** после подтверждения показывает
код: действующий сохраняется, использованный, истёкший или отозванный заменяется. CLI `users
invitation ensure` прежний код не заменяет: он завершает сохранённую выдачу. Для явной замены нужен
`users invitation new`; после потери ответа продолжайте через `ensure`. Состояния `unknown` и
`unavailable` не дают основания показывать или заменять код. Подробности — [пользователи и приглашения](users.md#регистрация-и-список). Список показывает автора, использование и срок без
служебных ID. Новый пароль вводится скрыто дважды в UI; готовить JSON-файл не нужно. После установки
мастер предлагает показать пароль администратора и открыть управление пользователями. В разделе
**Администраторы и доступ** можно выдать права зарегистрированному личному аккаунту, отозвать их,
показать или изменить пароль первоначального администратора.

Редактирование сайта и настроек сохраняет приватный черновик; отмена не
применяет изменения к работающему серверу.

Для подключённой Compose-установки используется то же меню с доступными ей
операциями. Дополнительные компоненты подключаются из отдельного раздела.
Неподдерживаемые команды обычного установщика в меню не показываются.

Формы внешних копий, PostgreSQL, Vault и смены ключей S3 создают приватные
файлы сами. Заявки, известные серверы и аккаунты выбираются из списка;
при недоступности списка можно ввести имя вручную. Расписание выбирается
по дням, времени и часовому поясу; произвольное выражение systemd также доступно.
В настройках работающего сервера можно изменить поддержанные параметры
Google/Telegram и прокси; секреты в итоговом просмотре скрыты.

После прерванного применения конфигурации мастер открывает восстановление.
После восстановления копии он сохраняет выбранный каталог назначения и предлагает
активацию через меню. Подтверждения остановки источника и сохранения внешнего MAS
остаются обязательными. Известные проверенные копии выбираются по сохранённым
метаданным; для принесённого отдельно архива нужен доверенный SHA256.

При обновлении из распакованного комплекта UI предлагает этот комплект после
проверки. После успешного применения можно открыть новый интерфейс без ручного
повторного ввода команды. `update latest` самостоятельно загружает стабильный релиз.

Оформление использует Rich; пакет `python3-rich` устанавливается вместе с
зависимостями обычного установщика. Для оформления до установки или на
подключённом стеке установите `sudo apt-get install python3-rich`.
В обычном SSH-терминале мастер и все меню открываются на весь экран; переход
в раздел или к настройке заменяет предыдущую страницу. Если пункты не помещаются,
`>` и `<` переключают страницы; номера действий сохраняются. Текущие значения
настроек остаются видимыми. Во время загрузки и операций без запроса ввода
показывается анимированный индикатор ожидания.
При запуске сервера в нём остаются текущий этап, процент завершённых этапов
и прошедшее время. Этапы имеют разную длительность; процент не оценивает
оставшееся время. Для загрузки Docker показываются объём и процент слоя,
а после двух измерений — его скорость. Ошибка команды останавливает запуск;
ожидание команд и готовности служб ограничено таймаутами. После ошибки
проверьте состояние, исправьте указанную причину и повторите запуск в том же
каталоге, сохраняя ключи и данные.
После выхода, в том числе при прерывании, возвращается прежний экран
терминала. Итог установки и результаты операций остаются видимыми до Enter.
Управление сохраняется: номера, Enter и команды ввода.
`sudo misbot ui --plain` использует последовательный вывод без оформления.
Без Rich и при `TERM=dumb` работает простой вывод. `NO_COLOR=1` отключает цвет
и полноэкранный режим. В простом режиме ожидание обозначается одной строкой.
В разделе **О программе и лицензиях** указаны авторы и пути к текстам лицензий.
Misbot и Rich используют MIT; [лицензии компонентов](third-party-licenses.md)
входят в установочный архив.

«Показать состояние» перечисляет работающие и остановленные службы, отмечает незавершённую установку
и занятую операцию. «Проверить доступность» сразу показывает результат каждой проверки DNS, HTTPS,
служб авторизации и звонков. Вход в клиент, зашифрованную переписку и реальный звонок проверяйте
отдельно по [проверкам после запуска](README.md#6-запуск-и-проверка). Длинные списки разделяются на
страницы с переходами `>` и `<`. Успешный DNS не означает готовность HTTPS. Отказы соединения,
таймауты, сертификаты, HTTP-коды и неверное содержимое ответа различаются. CLI сохраняет JSON и коды
завершения; `failure_code` уточняет причину без вывода ответов сервера или секретов.

В разделе «Медиа и базы данных» проверка S3 и перенос медиа запрашивают бакет,
endpoint, регион, каталог объектов и ключи доступа прямо в UI. Ключи скрыты
при вводе и в итоговом списке. «Назад», «отмена» и `0` отменяют заполнение.
В мастере новой установки они возвращают к выбору хранилища медиа,
сохраняя прежние настройки. Ctrl+C прерывает
операцию и дожидается завершения её процессов. Временный приватный профиль
удаляется после операции. Если провайдер не подтвердил удаление проверочного
объекта, сохраняются только необходимые материалы продолжения; интерфейс
показывает точную команду очистки. Она остаётся доступна через CLI.

## Справка и команды

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

Группы: `config`, `admin`, `users`, `storage`, `backup`, `update`, `credentials`, `federation`,
`homepage`; `features` относится к подключённой Compose-установке. Имя группы без действия открывает
её справку: `misbot backup schedule`, `misbot storage s3`. Исключение — `update`: эта команда
применяет локальный комплект. Можно также использовать `misbot help update`, `misbot update help`
или `misbot update --help`. У конкретной операции справка показывает назначение, обязательные и
дополнительные аргументы, пример, условия и восстановление. Описания переносятся по ширине
терминала; длинные примеры используют `\` для продолжения строки и сохраняют смысл при копировании.
Справка работает без Rich, root, Docker и чтения приватных настроек. `federation help` описывает
локальную политику, заявки и комнаты бота; `homepage help` описывает лендинг, два режима
переадресации и восстановление. Доступные операции и обязательные аргументы показаны в help каждой
группы и в `COMMAND --help`. Для команды восстановления следуйте выводу `status`, учитывая pending и
занятые блокировки. Не удаляйте checkpoints, не выполняйте повторный init на рабочей установке.

## Обновление и системная команда

Для обновления используйте доверенный распакованный комплект:

```bash
sudo misbot update apply --source /root/matrix-installer
```

Только после прерывания этого обновления выполните продолжение из того же комплекта:

```bash
sudo misbot update recover --source /root/matrix-installer
```

Из распакованного полного или компактного комплекта выполните
`sudo python3 misbot.py update`. Источник — запущенный комплект; цель — установка
рядом с командой, сохранённый системный каталог или проверенный `/opt/matrix-stack`.
Явный `--directory PATH` имеет приоритет. Если цель не найдена, команда спрашивает
путь в терминале либо показывает подсказку с `--directory`; неверный сохранённый
каталог требует явного исправления и не заменяется другой установкой.
`sudo misbot update --source PATH` принимает внешний каталог любого комплекта,
в том числе компактного. `update` — сокращение `update apply`, без загрузки из сети.
`update --help`, `update help` и `help update` только открывают справку.

`update check` проверяет GitHub Releases; `update latest` загружает последний стабильный комплект
для обычной установки и проверяет допустимость перехода. Если релиз несовместим, команда отказывает;
прежний совместимый выпуск автоматически не подбирается. `update apply --source PATH` применяет
доверенный распакованный комплект; `update recover --source PATH` восстанавливает прерванное
обновление.

Успешный start, update и update-recover обновляют wrapper. `config command install`
повторно регистрирует существующую установку; для старой установки, где команды
ещё нет: `sudo python3 /opt/matrix-stack/command_wrapper.py install --directory /opt/matrix-stack`.
`config command remove` удаляет только wrapper. `remove` открывает интерактивный
мастер необратимого удаления выбранных данных и внешних ресурсов. Утилита остаётся;
нет флага `--yes`, требуется итоговое подтверждение доменом. Работает после init.
Подробно: [удаление](README.md#11-панель-приветствия-и-полное-удаление).

### Черновик и повторная установка

`sudo misbot config draft show` показывает путь и этап черновика без секретных значений. `sudo
misbot config draft reset --confirm discard-draft` удаляет только текущий черновик, сохраняя
установку и checkpoints.

Если каталог установки удалён вручную, системная команда и её запись могут остаться.
`config command remove` может удалить принадлежащую этой установке команду без
`installation.json`; файлы установки и контейнеры эта операция не удаляет.
Для новой установки запустите `sudo python3 misbot.py ui` из проверенного
распакованного комплекта. Если сохранился черновик проверки уже удалённой установки,
мастер предложит проверить прежние параметры с начала или создать новый черновик.
Второй вариант сохраняет прежний JSON приватно в каталоге `previous` рядом с черновиком.
Старые данные автоматически не очищаются; нужен пустой каталог установки.

На новом сервере restore/fetch выполняйте через `sudo python3 misbot.py` из
распакованного комплекта. До регистрации команды так же выполняйте verify/status/
activate, явно задавая `--directory`. Восстановление копирует текущий совместимый
runtime, а активация регистрирует системную команду после успешного запуска.
Если команда недоступна или активация ещё не завершилась, продолжайте через
проверенный распакованный комплект с явным `--directory`, сохраняя данные
и identity. Сама стадия restore до активации команду не регистрирует.

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
На Ubuntu/Debian проверка автоматически устанавливает отсутствующую поддержку S3
из системных пакетов. Для подготовки нужны root и доступ к репозиториям пакетов;
при ошибке реквизиты сохраняются для повторной проверки.

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

Таблица сопоставляет зарегистрированные пути CLI и backend. Это справочник, а не последовательность
выполнения: изменение, отмена и восстановление выбираются по состоянию установки. Не запускайте
соседние строки подряд. Параметры ниже передаются backend; дополнительные флаги смотрите в справке
операции. Для команд с формами и сохранёнными профилями их можно опустить согласно справке `misbot
КОМАНДА --help`; недостающие значения запрашиваются только в TTY.

| CLI | Backend | Аргументы операции |
|---|---|---|
| `misbot config draft show` | `draft-show` | нет |
| `misbot config draft reset` | `draft-reset` | `--confirm` |
| `misbot admin bot show` | `admin-bot-show` | нет |
| `misbot admin bot set` | `admin-bot-set` | `--tag`; необязательные `--administrator` |
| `misbot admin access list` | `admin-access-list` | нет |
| `misbot admin access add` | `admin-access-add` | `--mxid` |
| `misbot admin access remove` | `admin-access-remove` | `--mxid` |
| `misbot admin access recover` | `admin-access-recover` | нет |
| `misbot update check` | `release-check` | нет |
| `misbot update latest` | `release-update` | нет |
| `misbot features status` | `features-status` | нет |
| `misbot features enable` | `features-enable` | `--config` |
| `misbot features recover` | `features-recover` | нет |
| `misbot storage status` | `storage-status` | нет |
| `misbot users invitation list` | `invitation-list` | нет |
| `misbot federation servers` | `federation-servers` | нет |
| `misbot federation check` | `federation-check` | `--server` |
| `misbot federation add` | `federation-add` | `--server` |
| `misbot federation remove` | `federation-remove` | `--server` |
| `misbot federation invite` | `federation-invite` | `--server` |
| `misbot federation api status` | `federation-api-status` | нет |
| `misbot federation api enable` | `federation-api-enable` | нет |
| `misbot federation api disable` | `federation-api-disable` | нет |
| `misbot federation peers` | `federation-peers` | нет |
| `misbot federation requests` | `federation-requests` | нет |
| `misbot federation requests clear` | `federation-clear-rejected` | нет |
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
## Главная страница

`misbot homepage help`: `show`, `check`, `apply`, `recover`, `rollback`. По умолчанию локальный лендинг. `sudo misbot homepage apply --mode element` выбирает Element; `sudo misbot homepage apply --mode external --url https://community.example.net/` — внешний сайт; `sudo misbot homepage apply --mode landing` возвращает лендинг. Поддерживаются `--name`, `--description`, `--support-url`; неуказанные поля сохраняются. Предварительно выполните ту же команду с `check` вместо `apply`. В обычной установке пересоздаётся только Caddy, результат проверяется по HTTPS. В `existing-compose` используются reload и отдельный набор проверок. Внешний сайт не проверяется. [Настройки и восстановление, включая различия режимов](homepage.md).


## Панель и GitHub Releases

`misbot admin bot show`; до создания `misbot admin bot set --tag admin-panel`.
Существующий тег неизменяем. Root может выбрать владельца через `--administrator`
и затем `misbot start` применяет конфигурацию. Для зарегистрированных личных аккаунтов:
`misbot admin access list`, `misbot admin access add --mxid @personal:example.org`,
`misbot admin access remove --mxid @personal:example.org`; прерванное изменение —
`misbot admin access recover`. Добавление даёт права MAS admin и доступ к панели,
сохраняя первоначального администратора. [Панель](admin-panel.md).
`misbot update check` сравнивает версии; `misbot update latest` скачивает готовые
release assets, проверяет checksum, manifest, версию и диск и запускает update backend.
Если применение не подтверждено, `update latest` возвращает ненулевой код завершения,
даже когда дочерний процесс завершился без ошибки. В результате остаются фактически
установленная версия и путь к приватному журналу. Уже установленный последний
релиз — успешная проверка без повторного обновления.
`misbot remove --backup-config PATH` добавляет известный профиль внешних копий
к интерактивному выбору удаления. Все эти операции требуют root.

Для установки с внешним MAS копия содержит только Synapse и локальные файлы.
После восстановления дополнительное подтверждение активации:
`sudo misbot backup activate --confirm source-stopped --external-mas-confirm preserved-state`.
Перед этим отдельно сохраните или восстановите прежние аккаунты, устройства
и ключи внешнего MAS. Подробности — [копии при внешнем MAS](backup.md#копии-при-внешнем-mas).

## Как читать результаты

SSH-меню сохраняет итог операции после закрытия индикатора: результат, известную
причину ошибки и доступное продолжение. Сообщение о прерывании или неподтверждённом
завершении не означает откат изменений. Перед повтором проверьте состояние и
следуйте указанной команде восстановления.

Проверка загрузки и целостности внешней копии не заменяет проверку восстановления
баз данных. Эти результаты показываются отдельно. Дата ранее проверенной копии
относится именно к той проверке; она не подтверждает новую копию. Если сведений
нет, интерфейс сообщает об этом, не объявляя операцию успешной или функцию
отключённой. Машинный вывод backend-команд сохраняет существующие JSON/JSONL
контракты; оформление SSH-меню не меняет их.
