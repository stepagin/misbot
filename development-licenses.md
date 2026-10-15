# Лицензии зависимостей разработки

Misbot распространяется под [MIT](LICENSE). Инструменты ниже используются
для проверки исходников, не копируются в установочный архив и не требуются
на установленном сервере. Они сохраняют собственные лицензии и уведомления
в установленных пакетах. Этот перечень помогает найти сведения о закреплённой
среде проверок; условия использования определяются полными текстами лицензий
соответствующих версий.

| Инструмент | Версия | Лицензия и источник |
|---|---|---|
| Ruff | 0.14.14 | [MIT](https://github.com/astral-sh/ruff/blob/0.14.14/LICENSE) |
| mypy | 1.19.1 | [MIT и уведомление PSF для включённого кода](https://github.com/python/mypy/blob/v1.19.1/LICENSE) |
| ESLint | 10.12.0 | [MIT](https://github.com/eslint/eslint/blob/v10.12.0/LICENSE) |
| Prettier | 3.8.1 | [MIT и лицензии включённых parsers](https://github.com/prettier/prettier/blob/3.8.1/LICENSE) |
| globals | 16.5.0 | [MIT](https://github.com/sindresorhus/globals/blob/v16.5.0/license) |

Версии Python-инструментов и тестовых библиотек закреплены вместе с hashes
в [ci/requirements.txt](ci/requirements.txt); исходный список —
[ci/requirements.in](ci/requirements.in). JavaScript-инструменты перечислены в
[package.json](package.json), полный набор — в [package-lock.json](package-lock.json).
Инструмент обновления lock-файла в этих manifests не закреплён и не входит
в этот перечень. Зависимости, устанавливаемые внутри runtime-образов ботов,
определены отдельно в их `package-lock.json` и `requirements.txt`.
Уведомление Rich сохранено в
[комплекте установщика](installer/third-party-licenses.md).

Транзитивные Python-зависимости инструментов: mypy-extensions и librt — MIT,
typing-extensions — PSF-2.0, pathspec — MPL-2.0. Уведомления сохранены в
`*.dist-info/licenses/` установленных wheels. Лицензии остальных библиотек
закреплённой тестовой среды:

| Лицензия | Пакеты |
|---|---|
| MIT | PyYAML, jmespath, charset-normalizer, markdown-it-py, mdurl, Rich, six, urllib3 |
| Apache-2.0 | boto3, botocore, requests, s3transfer |
| Apache-2.0 или BSD-3-Clause | cryptography |
| BSD-3-Clause | idna, pycparser |
| BSD-2-Clause | Pygments |
| MIT-0 | cffi |
| MPL-2.0 | certifi |
| Apache-2.0 или BSD-3-Clause | python-dateutil |

Поле `license` в JavaScript lock содержит SPDX-обозначения: MIT, Apache-2.0,
BSD-2-Clause, BSD-3-Clause, ISC либо BlueOak-1.0.0.
minimatch 10.2.6 сохраняет [Blue Oak Model License 1.0.0](https://blueoakcouncil.org/license/1.0.0)
в своём `LICENSE.md`. Prettier дополнительно сохраняет
уведомления включённых parsers в своём `LICENSE`. Указанные Python-библиотеки
с MPL относятся к среде проверок и не включаются в установочный архив. Пакеты с теми же именами, устанавливаемые отдельно на
сервере или в образе, сохраняют собственные уведомления.
