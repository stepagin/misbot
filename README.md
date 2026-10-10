# Misbot 1.1.0

Репозиторий содержит переносимый установщик нового Matrix-сервера, реферальный, административный и приветственный Matrix-боты, Telegram-компоненты. Установщик создаёт новую систему с Synapse, MAS, PostgreSQL, Element Web, Element Admin и MatrixRTC. Он не импортирует пользователей и переписки существующего сервера.

Существующий Compose-стек можно подключить после индивидуального аудита и
репетиции восстановления через [режим existing-compose](installer/existing-compose.md).
Он сохраняет конфигурацию и версии; доступные команды перечислены в `misbot help`.

## Установка на новый сервер

После публикации релиза скачайте **готовые** `matrix-installer.tar.gz` и
`matrix-installer.tar.gz.sha256` из [GitHub Releases](https://github.com/stepagin/misbot/releases).
GitHub Actions собирает их из тега; пользователю не нужны Git, Node.js или локальная
сборка исходников. Пока релиз не опубликован, владелец может собрать комплект
командой `python3 installer/build.py` и передать архив на сервер.

Например, после публикации тега `1.1.0`:

```bash
curl -fLO https://github.com/stepagin/misbot/releases/download/1.1.0/matrix-installer.tar.gz
curl -fLO https://github.com/stepagin/misbot/releases/download/1.1.0/matrix-installer.tar.gz.sha256
```

Сверьте checksum с опубликованным владельцем значением; затем распакуйте архив
по инструкции ниже. Python 3 нужен только на сервере для запуска утилиты.

Нужны Linux VPS (Debian/Ubuntu), права root/sudo, SSH, публичный IPv4, домен с DNS для основного адреса и поддоменов. Основная конфигурация — локальная PostgreSQL, локальные медиа либо S3, защищённые внешние копии. Основной маршрут не требует Vault, Google, Telegram или внешнюю БД. Практический минимум для небольшого сообщества: **2 vCPU, 2 ГиБ RAM, SSD 20 ГиБ**, из них не менее 10 ГиБ свободно до загрузки образов. Для роста и звонков планируйте 4 ГиБ RAM и SSD 40 ГиБ; запас для медиа и обслуживания зависит от нагрузки. Требования DNS и портов: [установка](installer/README.md).

В каталоге с полученными файлами:

```bash
sha256sum -c matrix-installer.tar.gz.sha256
sudo install -d -m 700 /root/misbot-setup
sudo tar -xzf matrix-installer.tar.gz -C /root/misbot-setup
sudo sh -c 'cd /root/misbot-setup/matrix-installer && sha256sum -c SHA256SUMS'
sudo python3 /root/misbot-setup/matrix-installer/misbot.py ui
```

Ожидается `OK` для архива и каждого payload, затем русский SSH-мастер: домен, система/DNS, выбор медиа, необязательная авторизация/копии, итог, запуск и проверка. До установки команда запускается из комплекта; после init `misbot` доступен из любого каталога. Не выполняйте `init` заново ради исправления ошибки действующего сервера.

```bash
misbot version
sudo misbot status
sudo misbot check
sudo misbot ui
sudo misbot credentials admin show
sudo misbot users invitation show
sudo misbot users invitation status
```

Версия — `Misbot 1.1.0`. Пароль администратора и первый токен показываются только авторизованному оператору в терминале. Токен позволяет одну обычную регистрацию, default 7 дней; вводится в форме регистрации, никогда в URL. Если выдача ещё не завершена: `sudo misbot users invitation ensure`. Начальная страница основного домена — локальный лендинг с кнопками входа и регистрации. [Сайт и переадресация](installer/homepage.md).

В `status` сначала завершите указанную pending операцию по точной recovery-команде. Неверную конфигурацию исправьте в приватном файле и повторите `sudo misbot config validate`; lock и данные не удаляйте. S3 можно отложить и выбрать локальные медиа; позднее `sudo misbot storage s3 connect --config /root/misbot-s3.env` переносит существующие файлы. [Диагностика S3](installer/cli.md#проверка-s3-без-подключения).

Для эксплуатации и восстановления:

```bash
sudo misbot backup create
sudo misbot backup verify
sudo misbot backup key create --key /root/misbot-backup.key
sudo misbot backup external create --config /root/misbot-backup-s3.env --key /root/misbot-backup.key
sudo misbot update apply --source /root/misbot-setup/matrix-installer
sudo misbot stop
sudo misbot start
sudo misbot remove
```

Внешняя копия зашифрована; ключ держите вне установки и отдельно от объекта копии. Обновление принимает проверенный совместимый комплект, сохраняет данные и имеет recovery. `remove` открывает многоэтапный мастер удаления: данные/секреты/копии, S3 со всеми версиями в выбранном prefix, принадлежащие установщику внешние БД и Vault. После итогового подтверждения выбранные данные удаляются; **утилита сохраняется**. Мастер работает и сразу после `init`. Восстановление возвращает состояние на момент копии; остановите источник перед активацией восстановленного экземпляра.

Подробно: [CLI](installer/cli.md), [UI и конфигурация](installer/configuration.md), [пользователи](installer/users.md), [администратор и первый токен](installer/configuration.md#администратор-и-первый-токен), [ежедневное управление](installer/administration.md), [федерация и оба бота](installer/federation.md), [копии и восстановление](installer/backup.md), [credentials](installer/credentials.md), [обновление и recovery](installer/README.md#10-обновление). `dist/` создаётся локально и не хранится в Git.

## Структура исходников

| Путь | Назначение |
| --- | --- |
| [`installer/`](installer/) | Переносимый установщик, состав архива и тесты |
| [`bots/telegram/`](bots/telegram/) | Telegram-бот и общая управляющая служба |
| [`bots/matrix-referral/`](bots/matrix-referral/) | Matrix-бот приглашений |
| [`bots/matrix-federation/`](bots/matrix-federation/) | Отдельный административный Matrix-бот |
| [`installer/referral-control/`](installer/referral-control/) | Уникальные файлы сборки и настройки управляющей службы |
| [`deployment/`](deployment/) | Универсальные правила эксплуатации, миграции и инструмент зеркалирования истории |
| `dist/` | Локальный результат сборки; Git его игнорирует |

`installer/build.py` берёт исходники из `bots/` и помещает их в архив. В распакованном архиве создаются каталоги `referral-bot/`, `federation-bot/` и `referral-control/`. Общие SDK pins и crypto helper имеют единственный источник в Git. Проверить актуальность манифеста без пересборки можно командой `python3 installer/build.py --check`.

Журналы действующего развёртывания, приватные настройки, реквизиты, резервные копии и серверная песочница исключены из публичного репозитория. Не добавляйте их содержимое или реальные значения в примеры, задачи и тесты.

## Локальные проверки

На Linux запускайте проверки изменённой части проекта:

```bash
python3 -m unittest discover -s installer/tests -p 'test_*.py'
python3 -m unittest discover -s bots/telegram/tests -p 'test_*.py'
(cd bots/matrix-referral && npm test)
node --test bots/matrix-federation/tests/*.test.js
python3 installer/build.py --check
python3 -m unittest discover -s deployment/tests -p 'test_*.py'
bash -n installer/install.sh
```

Интеграционные тесты установщика требуют отдельной контейнерной среды. Локальные тесты не подтверждают работу реальных DNS, OAuth-входа, федерации и звонков; завершите проверки из инструкции на целевом сервере.

Начните установку через SSH-мастер: в распакованном комплекте `sudo python3 misbot.py ui`, после установки `sudo misbot ui` (или `sudo misbot` в TTY). Проверка настроек: `sudo misbot config validate`. [Конфигурация](installer/configuration.md) и [первый токен](installer/configuration.md#администратор-и-первый-токен).

[Управление федерацией](installer/federation.md): `sudo misbot federation help` или раздел SSH UI «федерация/бот/заявки».
[Административная панель](installer/admin-panel.md) — `@admin-panel:<домен>`, отвечает только владельцу в личном зашифрованном чате: `помощь`, `версия`, `обновить`, `статистика день`, `рассылка`, `приветствие`, `уведомления`. Редкие команды федерации: `ещё`. Настройки тега сохраняются локально; существующий тег при обновлении сохраняется.
`@server-guide:<домен>` приветствует новых пользователей, доставляет объявления и молча игнорирует входящие сообщения. Автоматические уведомления о новых minor-версиях, регистрациях и ежемесячная статистика отключаются независимо. Число/длительность звонков MatrixRTC недоступны; ограничения счётчика E2EE-событий описаны в документации.
Реферальный бот: `пригласить`, `токены`, `отозвать`, `помощь`.
HTTPS discovery `/.well-known/misbot`, [протокол API](installer/misbot-api.openapi.json) на основном домене; заявки требуют независимого местного одобрения обеих сторон.

## Условия распространения и границы проверки

Лицензия исходников пока не выбрана владельцем. До публикации необходимо добавить
лицензию и определить условия распространения; доступность исходников сама по себе
не предоставляет разрешения на их использование и распространение.

Локальная сборка и изолированные проверки не завершают приёмку: остаются открытыми
полный проход установки с публичным HTTPS, браузерный вход и регистрация,
восстановление зашифрованной внешней копии и полный сценарий обновления с recovery.
Публичная межсерверная федерация, внешний OAuth и звонки также не подтверждены.
