# Misbot 1.2.0

Misbot помогает установить и обслуживать Matrix-сервер для своего сообщества:
общаться в Element, приглашать новых пользователей и управлять сервером через
SSH-мастер или административного бота. Для регистрации нужен код-приглашение;
его вводят в форме регистрации.

Установщик предназначен для нового сервера. Он не импортирует пользователей и
переписки существующей установки.

Существующий Compose-стек можно подключить после индивидуального аудита и
репетиции восстановления через [режим existing-compose](installer/existing-compose.md).
Он сохраняет конфигурацию и версии; доступные команды перечислены в `misbot help`.

## Установка на новый сервер

Скачайте `matrix-installer.tar.gz` и
`matrix-installer.tar.gz.sha256` из [GitHub Releases](https://github.com/stepagin/misbot/releases).
Сверьте контрольную сумму с опубликованным значением. Для запуска утилиты на
сервере нужен Python 3.11 или новее.

Нужны Linux VPS (Debian/Ubuntu), права root/sudo, SSH, публичный IPv4 и домен с
доступом к DNS. Практический минимум для небольшого сообщества — **2 vCPU,
2 ГиБ RAM, SSD 20 ГиБ**, из них не менее 10 ГиБ свободно до установки.
Для роста и звонков планируйте 4 ГиБ RAM и SSD 40 ГиБ; запас для медиа и
обслуживания зависит от нагрузки. Требования DNS и портов — в
[инструкции установки](installer/README.md).

В каталоге с полученными файлами:

```bash
sha256sum -c matrix-installer.tar.gz.sha256
sudo install -d -m 700 /root/misbot-setup
sudo tar -xzf matrix-installer.tar.gz -C /root/misbot-setup
sudo sh -c 'cd /root/misbot-setup/matrix-installer && sha256sum -c SHA256SUMS'
sudo apt-get update
sudo apt-get install -y python3 python3-yaml python3-rich dnsutils iproute2
sudo python3 /root/misbot-setup/matrix-installer/misbot.py ui
```

Проверка должна показать `OK` для архива и каждого файла. Затем SSH-мастер
поможет указать домен, проверить DNS, выбрать хранение медиа, настроить вход и
резервные копии, запустить сервер. После инициализации команда `misbot` доступна
из любого каталога. При ошибке действующей установки следуйте `misbot status`;
повторный `init` не нужен.

`misbot ui` предлагает разделы по задачам: состояние сервера, приглашения,
копии, хранилище, сайт, федерация и обновление. Перед изменением показывает
параметры и просит подтверждение. Для оформления используется Rich (MIT);
без него доступен простой вывод. Подробности — в
[описании интерфейса](installer/cli.md#управление-через-терминал), лицензия — в
[сведениях о компонентах](installer/third-party-licenses.md).

```bash
misbot version
sudo misbot status
sudo misbot check
sudo misbot ui
sudo misbot credentials admin show
sudo misbot users invitation show
sudo misbot users invitation status
```

Пароль администратора и первый код-приглашение доступны оператору в терминале.
Первый код позволяет одну регистрацию и по умолчанию действует 7 дней.
Если выдача ещё не завершена, выполните `sudo misbot users invitation ensure`.
На основном домене открывается страница с кнопками входа и регистрации.
[Настройка главной страницы](installer/homepage.md).

Если `status` сообщает о незавершённой операции, выполните указанную команду
восстановления. Для проверки настроек используйте `sudo misbot config validate`.
Можно начать с локальных медиа и позднее подключить S3:
`sudo misbot storage s3 connect --config /root/misbot-s3.env`.
[Диагностика S3](installer/cli.md#проверка-s3-без-подключения).

## Общение и управление

В Element X укажите основной домен сервера; браузерный чат доступен по адресу
`https://web.<домен>`. Для нового аккаунта создайте учётную запись с
кодом-приглашением, затем войдите в клиент. С существующим аккаунтом сразу
откройте чат.

- Реферальный бот `@referral-bot:<домен>` выдаёт приглашения: `пригласить`,
  `токены`, `отозвать`, `помощь`.
- [Административная панель](installer/admin-panel.md) `@admin-panel:<домен>`
  отвечает владельцу в личном зашифрованном чате: `помощь`, `версия`, `обновить`,
  `статистика день`, `рассылка`, `приветствие`, `уведомления`.
- `@server-guide:<домен>` приветствует новых пользователей и доставляет
  объявления.
- [Федерация](installer/federation.md) подключает другие серверы после
  независимого одобрения обеих сторон. В SSH-мастере выберите
  «федерация/бот/заявки» или выполните `sudo misbot federation help`.

## Обслуживание

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

Внешняя копия зашифрована; ключ держите отдельно от установки и самой копии.
Обновление сохраняет данные и позволяет восстановить работу после сбоя.
`remove` открывает мастер удаления выбранных данных и внешних ресурсов;
после итогового подтверждения удаление необратимо, утилита сохраняется.
Восстановление возвращает состояние на момент копии. Остановите исходный сервер
перед активацией восстановленного экземпляра.

Подробно: [команды](installer/cli.md), [мастер и настройки](installer/configuration.md),
[пользователи](installer/users.md), [ежедневное управление](installer/administration.md),
[копии и восстановление](installer/backup.md), [пароли и ключи доступа](installer/credentials.md),
[обновление](installer/README.md#10-обновление).

## Структура исходников

| Путь | Назначение |
| --- | --- |
| [`installer/`](installer/) | Переносимый установщик, состав архива и тесты |
| [`bots/telegram/`](bots/telegram/) | Telegram-бот |
| [`bots/matrix-referral/`](bots/matrix-referral/) | Matrix-бот приглашений |
| [`bots/matrix-federation/`](bots/matrix-federation/) | Отдельный административный Matrix-бот |
| [`deployment/`](deployment/) | Универсальные правила эксплуатации, миграции и инструмент зеркалирования истории |

Для сборки из исходников выполните `python3 installer/build.py` из корня
репозитория. Архив и контрольная сумма появятся в `dist/`.

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

## Лицензия

Автор Misbot — **Stepan Ponamarev**. Исходники распространяются под [лицензией MIT](LICENSE).
Код можно свободно использовать, изменять и распространять, в том числе
коммерчески, при сохранении текста лицензии и уведомления об авторских правах.
Сторонние компоненты сохраняют собственные лицензии.

## Ограничения

Полная установка с публичным HTTPS, браузерный вход и регистрация,
восстановление внешней копии, обновление после сбоя, публичная федерация,
внешний OAuth и звонки пока не подтверждены на действующем сервере.
