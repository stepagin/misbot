# Разработка Misbot

Проверки исходников рассчитаны на Python 3.11–3.14 и Node 24. Runtime использует
системные Python-пакеты и зависимости из каталогов ботов; зависимости разработки
из корня проекта и `ci/` не входят в установочный архив.

Из корня репозитория создайте отдельную среду:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r ci/requirements.txt
npm ci --ignore-scripts
```

Проверки оформления и базовых ошибок:

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
npm run format:check
npm run lint
```

Для применения оформления: `.venv/bin/ruff format .` и `npm run format`.
Python сохраняет четыре пробела и одинарные кавычки; JavaScript — CommonJS,
четыре пробела, двойные кавычки и точки с запятой. Начальный набор правил выявляет
ошибки синтаксиса, неизвестные имена, повторяющиеся ключи и опасное управление
потоком. Проверки runtime-входа обязательны независимо от аннотаций типов.

Тесты запускаются из той же среды:

```bash
.venv/bin/python -m unittest discover -s installer/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s bots/telegram/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s deployment/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s ci/tests -p 'test_*.py'
npm test --prefix bots/matrix-referral
node --test bots/matrix-federation/tests/*.test.js
.venv/bin/python installer/build.py --check
bash -n installer/install.sh
```

Контейнерные интеграционные проверки требуют отдельной изолированной среды;
локальные тесты не подтверждают реальный OAuth, федерацию и звонки.
После изменения упакованных файлов `python3 installer/build.py` обновляет
`installer/SHA256SUMS` и локальный архив `dist/matrix-installer.tar.gz`.

Версии Python-инструментов и тестовых библиотек закреплены вместе с транзитивными
зависимостями и SHA256 в `ci/requirements.txt`, JavaScript-инструментов —
в корневом `package-lock.json`. Для обновления Python lock используется
`uv 0.9.26 pip compile ci/requirements.in --universal --python-version 3.11
--generate-hashes --no-header --output-file ci/requirements.txt`.

Misbot сохраняет [MIT](LICENSE). Лицензии инструментов: [сведения о зависимостях
разработки](development-licenses.md); лицензии включённых компонентов:
[уведомления установщика](installer/third-party-licenses.md).
