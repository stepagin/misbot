# Misbot 1.0.0

Репозиторий содержит переносимый установщик нового Matrix-сервера, реферальный и административный Matrix-боты, Telegram-компоненты. Установщик создаёт новую систему с Synapse, MAS, PostgreSQL, Element Web, Element Admin и MatrixRTC. Он не импортирует пользователей и переписки существующего сервера.

## Установка на новый сервер

Из корня репозитория соберите архив с Python 3:

```bash
python3 installer/build.py
```

Команда создаст `dist/matrix-installer.tar.gz`. Передайте архив на новый сервер и следуйте [инструкции установки](installer/README.md). В ней описаны DNS, порты, внешние OAuth-настройки, запуск, проверки, резервное копирование и восстановление. `dist/` создаётся локально и не хранится в Git.

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

Начните установку через SSH-мастер: в распакованном комплекте `sudo python3 misbot.py ui`, после установки `sudo misbot ui` (или `sudo misbot` в TTY). Проверка настроек: `sudo misbot config validate`. [Конфигурация и первый токен](installer/configuration.md).

[Управление федерацией](installer/federation.md): `sudo misbot federation help` или раздел SSH UI «федерация/бот/заявки».
Административный бот: `помощь`, `серверы`, `пригласить сервер example.org`, `заявки`.
Реферальный бот: `пригласить`, `токены`, `отозвать`, `помощь`.
HTTPS discovery `/.well-known/misbot`, [протокол API](installer/misbot-api.openapi.json) на основном домене; заявки требуют независимого местного одобрения обеих сторон.
