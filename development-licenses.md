# Лицензии зависимостей разработки

Misbot распространяется под [MIT](LICENSE). Инструменты ниже используются
для проверки исходников, не копируются в установочный архив и не требуются
на установленном сервере. Они сохраняют собственные лицензии и уведомления
в установленных пакетах; изменение оформления и типов не заменяет эти уведомления.

| Инструмент | Версия | Лицензия и источник |
|---|---|---|
| Ruff | 0.14.14 | [MIT](https://github.com/astral-sh/ruff/blob/0.14.14/LICENSE) |
| mypy | 1.19.1 | [MIT и уведомление PSF для включённого кода](https://github.com/python/mypy/blob/v1.19.1/LICENSE) |
| ESLint | 9.39.2 | [MIT](https://github.com/eslint/eslint/blob/v9.39.2/LICENSE) |
| Prettier | 3.8.1 | [MIT и лицензии включённых parsers](https://github.com/prettier/prettier/blob/3.8.1/LICENSE) |
| globals | 16.5.0 | [MIT](https://github.com/sindresorhus/globals/blob/v16.5.0/license) |
| uv (обновление lock) | 0.9.26 | [MIT или Apache-2.0](https://github.com/astral-sh/uv/tree/0.9.26) |

Полные наборы зависимостей определены в `ci/requirements.txt` и корневом
`package-lock.json`. Runtime зависимости ботов остаются в их собственных lock
файлах. Уведомление Rich сохранено в
[комплекте установщика](installer/third-party-licenses.md).
