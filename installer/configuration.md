# Конфигурация и SSH-мастер Misbot 1.0.0

В распакованном комплекте: `sudo python3 misbot.py ui`. После установки:
`sudo misbot ui` или `sudo misbot` в TTY. Для другого каталога добавьте
`--directory '/opt/Matrix Test'`. В non-TTY используйте обычный CLI;
явный UI возвращает 2 и подсказку `ssh -t USER@HOST`.

Мастер использует простой русский текст без ANSI и curses, работает с
`TERM=dumb`, `NO_COLOR`, небольшим окном и изменением его размера.
Показывает этап, выбранную установку и описание поля. Enter сохраняет default
или уже настроенный секрет; секрет вводится без эха. `назад` возвращает шаг,
`отмена` прекращает ввод, `сохранить и выйти` сохраняет приватный draft.
Повтор `ui` продолжает draft, сохраняя внутренние ключи. Ctrl+C во время
операции передаётся backend; дождитесь завершения, затем следуйте `misbot status`.
При потере SSH дочерняя операция может ещё завершаться: busy не обходят удалением lock.

Мастер проверяет Linux amd64/systemd, память, диск, DNS/CAA, порты и маршруты.
Если отсутствуют зависимости: `sudo apt-get update`, затем
`sudo apt-get install -y python3 python3-yaml dnsutils iproute2` и повтор `ui`.
DNS не считается готовым по подтверждению пользователя. Подготовьте A-записи
всех поддоменов из [README](README.md); отключите DNS proxy и неподдерживаемые AAAA.

## Единственный источник настроек

Формат конфигурации **2** отделён от версии продукта **1.0.0**.
Вымышленные примеры находятся в `configuration-examples/*.env.example`.
Копируйте только нужную область в `settings/`, замените её значения и установите
`chmod 600` до валидации. Основные имена/defaults совпадают со схемой мастера.
В каталоге установки:

| Файл | Назначение | Когда нужен |
|---|---|---|
| `settings/server.env` | Домен, IPv4, email, режимы медиа/БД | Всегда |
| `settings/policy.env` | Сеть, федерация, регистрация и срок приглашений | Если defaults изменены |
| `settings/media-s3.env` | Шесть параметров S3 медиа | Выбраны S3-медиа или сохранены параметры подключения |
| `settings/auth.env` | Google/Telegram OIDC и Telegram egress | Выбран соответствующий провайдер |
| `settings/database.env` | Существующий внешний connector PostgreSQL | Используется дополнительная внешняя БД |
| `settings/backup.env` | Отдельный S3 профиль, recovery key и расписание | Настроены внешние копии |
| `secrets/` | Автоматически созданные внутренние пароли и токены | Backend; вручную не редактировать |
| `config/`, `compose.yaml`, `mas-proxy.env` | Generated runtime | Не являются пользовательскими шаблонами |

Каталог `settings` — root:root 0700, файлы — root:root 0600, обычные файлы,
без symlink/hardlink. Env читается буквально как UTF-8 `KEY=value`: без `export`,
shell-кода, подстановок и комментариев после значения. Внешние парные кавычки
снимаются; символы внутри остаются буквальными. Не выполняйте `source`.
Каждое поле принадлежит одной области; чужие, неизвестные и повторяющиеся поля
отвергаются. Defaults заполняет общая модель; файлы не перекрывают друг друга.
Профиль backup имеет собственные S3-поля и не перекрывает media.

Ручной путь:

```bash
sudo nano /opt/matrix-stack/settings/server.env
sudo misbot config validate
sudo misbot config apply
sudo misbot check
```

`validate` не подключается к Docker или внешним ресурсам и не выводит значения.
Его JSON содержит `stage`, `status`, `format`; при ошибке схемы — безопасные
`code`, `file`, `field`, `explanation`, `next_actions`. Exit code: 0 — готово,
2 — ошибка ввода, 1 — pending/runtime; прерывание конфигурационной операции — 130.
`apply` проверяет модель, создаёт согласованную копию, проверяет восстановление
обеих БД и вызывает существующий запуск. Возможна короткая остановка.
Если ввод неверен, сообщение называет файл/поле и способ повторной проверки;
неверные значения не публикуются мастером. После ручного редактирования до
успешной валидации не запускайте `apply`. Identity и установленное хранилище
меняются только отдельными поддерживаемыми операциями; DOMAIN/server_name
неизменяемы. Смена credentials — [отдельные операции](credentials.md).
Меню настроек разрешает только обычные изменяемые параметры; отмена сохраняет
действующую конфигурацию. При неудаче его применения возвращается checkpoint.

## Миграция и восстановление

Старый `settings.env` поддерживается без автоматической ротации. Сначала
обновите runtime через `misbot update apply --source /root/matrix-installer`,
затем выполните:

```bash
sudo misbot config validate
sudo misbot config migrate
sudo misbot config validate
sudo misbot status
```

Миграция проверяет принадлежность, immutable домен, pending состояния и схему;
сохраняет исходный файл приватно в `stages/configuration/ID/previous/settings.env`;
готовит полный новый набор, фиксирует `configuration-transition.json`, публикует
файлы, проверяет весь набор и удаляет старый действующий `settings.env`.
Пароли, ключи, БД, аккаунты, медиа и bot checkpoints не меняются.
Успех определяется проверкой всего набора. Во время незавершённой публикации
обычные операции отказываются читать настройки; все читатели используют общий lock.

После сбоя: `sudo misbot config recover`; для возврата приватной исходной копии
незавершённой публикации: `sudo misbot config rollback`. Повтор `migrate` после
успеха безопасен. При неизвестных полях миграция останавливается, сохранив файл;
разберите значение приватно, не удаляйте его без выяснения назначения.
После успеха редактируют только `settings/`; checkpoint остаётся для восстановления.

## S3 и внешние копии

Мастер использует [тот же диагностический backend](cli.md), включая безопасный
probe/cleanup. При отказе доступны исправление, повтор, локальные медиа до
подключения S3 или сохранение draft. Уже установленный S3 не переключается
на пустое локальное хранилище. Позднее подключение переносит существующие файлы:

```bash
sudo misbot storage s3 check --config /root/misbot-s3.env --purpose media
sudo misbot storage s3 connect --config /root/misbot-s3.env
```

Внешние копии можно явно отложить. Это не подтверждает защиту от потери VPS.
Мастер хранит отдельный профиль в `settings/backup.env`; диагностика с
`--purpose backup`, внешние копии и таймер читают его через общую схему. Recovery key
должен находиться вне каталога установки, иметь приватные права; сохраните его
в защищённом независимом месте. Команды ручной проверки/применения расписания
даны в [инструкции копий](backup.md). Смена хранилища и смена credentials отдельны.

## Администратор и первый токен

Backend автоматически создаёт `@matrix-admin:DOMAIN` с сохранённым паролем.
Вход: `https://web.DOMAIN`; реквизиты: `secrets/admin-access.json`.
Просмотр в root TTY: `sudo misbot credentials admin show`.
Смена: `misbot credentials admin set --help` (приватный файл, без пароля в argv).

После успешного запуска мастер отдельно выдаёт токен на **одного обычного**
пользователя, без административных прав. Default — **604800 секунд / 7 дней**;
`INVITATION_LIFETIME_SECONDS` изменяет срок будущей выдачи. Создание администратора
не расходует приглашение. Токен показывается оператору на экране завершения,
не включается в URL, JSON status или общие журналы; хранится приватно в
`secrets/first-registration.json`.

```bash
sudo misbot users invitation ensure  # создать/продолжить тот же intent
sudo misbot users invitation status  # valid, used, expired, revoked, pending/absent
sudo misbot users invitation show    # только root TTY, секрет не для журналов
sudo misbot users invitation revoke
sudo misbot users invitation new     # явная замена после отзыва прежнего
```

Повтор ensure/UI не заменяет использованный или истёкший токен. Потеря ответа
создания восстанавливается поиском сохранённого собственного токена до нового
POST. Прерванную явную замену продолжайте через `ensure`, сохраняя checkpoint.
Если MAS недоступен, сервер и ключи сохраняются; повторите `ensure`, а не init.
Состояние подтверждается MAS Admin API; временная недоступность не выдаётся за
«действителен». [Справочник MAS API](https://element-hq.github.io/matrix-authentication-service/topics/admin-api.html).

## Поля

Полная таблица общей схемы приведена ниже. Значения примеров вымышлены.
Необязательные значения могут быть пустыми; обязательность при выбранной функции
проверяет модель. Файлы для невыбранных ресурсов создавать не нужно.

| Файл / поле | Назначение | Обязательность | Формат / default | Вымышленный пример | Влияние |
|---|---|---|---|---|---|
| settings/server.env / `DOMAIN` | Основной домен, без https:// и пути | всегда | hostname без схемы/порта; пусто | example.org | Matrix identity: после init изменять нельзя. |
| settings/server.env / `PUBLIC_IP` | Публичный IPv4 из панели VPS; пусто — единственный A домена | необязательно | публичный IPv4; пусто | 203.0.113.20 | Применяется при generate/start; работающий сервер требует копии. |
| settings/server.env / `ACME_EMAIL` | Email оператора для сертификатов | необязательно | email; пусто | operator@example.org | Применяется при generate/start; работающий сервер требует копии. |
| settings/server.env / `MEDIA_MODE` | Медиа: local или s3 | необязательно | local/s3; local | local | Действующее хранилище меняется только storage operation. |
| settings/server.env / `POSTGRES_MODE` | PostgreSQL: local или external | необязательно | local/external; local | local | Действующее хранилище меняется только storage operation. |
| settings/policy.env / `DOCKER_SUBNET` | Приватная свободная IPv4 сеть /24 | необязательно | приватная IPv4 /24; 172.30.50.0/24 | 172.30.50.0/24 | После первого запуска не изменять. |
| settings/policy.env / `FEDERATION_SERVERS` | Разрешённые Matrix server_name через запятую | необязательно | непустые hostname через запятую; misbot.ru | matrix.org,example.net | Применяется при generate/start; работающий сервер требует копии. |
| settings/policy.env / `OPEN_REGISTRATION` | Разрешить новые парольные регистрации | необязательно | true/false; true | true | Применяется при generate/start; работающий сервер требует копии. |
| settings/policy.env / `REGISTRATION_TOKEN_REQUIRED` | Требовать приглашение при парольной регистрации | необязательно | true/false; true | true | Применяется при generate/start; работающий сервер требует копии. |
| settings/policy.env / `INVITATION_LIFETIME_SECONDS` | Срок приглашений и первого токена, секунды (7 дней) | необязательно | целое 1..315360000; 604800 | 604800 | Новые приглашения; сохранённый токен не продлевается. |
| settings/policy.env / `INVITATION_USAGE_LIMIT` | Лимит приглашений бота; первый токен всегда на одного | необязательно | целое 1..2147483647; 1 | 1 | Применяется при generate/start; работающий сервер требует копии. |
| settings/auth.env / `GOOGLE_CLIENT_ID` | Client ID из Google Cloud | вместе с secret | буквальная строка без control characters; пусто | FICTIONAL_GOOGLE_ID | Применяется при generate/start; работающий сервер требует копии. |
| settings/auth.env / `GOOGLE_CLIENT_SECRET` | Client secret из Google Cloud | вместе с ID | буквальная строка без control characters; пусто | FICTIONAL_GOOGLE_SECRET | В активной установке форма не ротирует OAuth credentials. |
| settings/auth.env / `TELEGRAM_CLIENT_ID` | Client ID приложения Telegram OIDC | вместе с secret | буквальная строка без control characters; пусто | FICTIONAL_TELEGRAM_ID | Применяется при generate/start; работающий сервер требует копии. |
| settings/auth.env / `TELEGRAM_CLIENT_SECRET` | Client secret Telegram OIDC | вместе с ID | буквальная строка без control characters; пусто | FICTIONAL_TELEGRAM_SECRET | В активной установке форма не ротирует OAuth credentials. |
| settings/auth.env / `SOCKS5_CONFIG` | Необязательный HTTPS-egress Telegram через SOCKS5 | необязательно | socks5://[user:password@]host:port; пусто | socks5://fictional:password@proxy.example.org:1080 | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_HOST` | Хост выделенной PostgreSQL | external | hostname или IP без схемы/порта; пусто | db.example.org | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_PORT` | Порт PostgreSQL | необязательно | целое 1..65535; 5432 | 5432 | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_SSL_MODE` | TLS PostgreSQL | необязательно | require/verify-full; require | verify-full | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_SSL_ROOT_CERT` | Путь CA PEM; пусто — системный CA | необязательно | абсолютный путь без symlink/..; пусто | /root/db-ca.pem | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_ADMIN_USER` | Административная роль выделенного кластера | необязательно | PostgreSQL identifier; postgres | postgres | Применяется при generate/start; работающий сервер требует копии. |
| settings/database.env / `POSTGRES_ADMIN_PASSWORD` | Пароль роли провайдера PostgreSQL | external | буквальная строка без control characters; пусто | FICTIONAL_DATABASE_PASSWORD | Через поддерживаемый backend; не менять обычной формой. |
| settings/media-s3.env / `S3_BUCKET` | Имя существующего bucket | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | fictional-private-bucket | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/media-s3.env / `S3_REGION` | Region подписи SigV4 из документации провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | us-east-1 | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/media-s3.env / `S3_ENDPOINT_URL` | HTTPS origin провайдера; пусто — AWS | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | https://s3.example.org | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/media-s3.env / `S3_PREFIX` | Отдельный prefix с / в конце | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | matrix/example/media/ | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/media-s3.env / `S3_ACCESS_KEY_ID` | Access key ID провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | FICTIONAL_ACCESS | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/media-s3.env / `S3_SECRET_ACCESS_KEY` | Secret access key провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | FICTIONAL_SECRET | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_BUCKET` | Имя существующего bucket | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | fictional-private-bucket | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_REGION` | Region подписи SigV4 из документации провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | us-east-1 | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_ENDPOINT_URL` | HTTPS origin провайдера; пусто — AWS | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | https://s3.example.org | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_PREFIX` | Отдельный prefix с / в конце | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | matrix/example/media/ | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_ACCESS_KEY_ID` | Access key ID провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | FICTIONAL_ACCESS | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `S3_SECRET_ACCESS_KEY` | Secret access key провайдера | s3, кроме необязательного endpoint | буквальная строка без control characters; пусто | FICTIONAL_SECRET | Назначение/credentials меняются отдельными storage/credentials операциями. |
| settings/backup.env / `BACKUP_KEY_FILE` | Приватный recovery key вне установки | backup включён | абсолютный путь без symlink/..; пусто | /root/misbot-backup.key | Применяется при generate/start; работающий сервер требует копии. |
| settings/backup.env / `BACKUP_CALENDAR` | Расписание systemd, необязательно | необязательно | буквальная строка без control characters; пусто | *-*-* 03:00:00 UTC | Применяется при generate/start; работающий сервер требует копии. |
